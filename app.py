import os
import json
import copy
import time
import random
import logging
import numpy as np
from typing import Any, Dict, List, Optional, Union

import torch
from PIL import Image
import gradio as gr

from diffusers import (
    DiffusionPipeline,
    AutoencoderTiny,
    AutoencoderKL,
    AutoPipelineForImage2Image,
    FluxPipeline,
    FlowMatchEulerDiscreteScheduler)

from huggingface_hub import (
    hf_hub_download,
    HfFileSystem,
    ModelCard,
    snapshot_download)

import spaces

def calculate_shift(
    image_seq_len,
    base_seq_len: int = 256,
    max_seq_len: int = 4096,
    base_shift: float = 0.5,
    max_shift: float = 1.16,
):
    m = (max_shift - base_shift) / (max_seq_len - base_seq_len)
    b = base_shift - m * base_seq_len
    mu = image_seq_len * m + b
    return mu

def retrieve_timesteps(
    scheduler,
    num_inference_steps: Optional[int] = None,
    device: Optional[Union[str, torch.device]] = None,
    timesteps: Optional[List[int]] = None,
    sigmas: Optional[List[float]] = None,
    **kwargs,
):
    if timesteps is not None and sigmas is not None:
        raise ValueError("Only one of `timesteps` or `sigmas` can be passed. Please choose one to set custom values")
    if timesteps is not None:
        scheduler.set_timesteps(timesteps=timesteps, device=device, **kwargs)
        timesteps = scheduler.timesteps
        num_inference_steps = len(timesteps)
    elif sigmas is not None:
        scheduler.set_timesteps(sigmas=sigmas, device=device, **kwargs)
        timesteps = scheduler.timesteps
        num_inference_steps = len(timesteps)
    else:
        scheduler.set_timesteps(num_inference_steps, device=device, **kwargs)
        timesteps = scheduler.timesteps
    return timesteps, num_inference_steps

# FLUX pipeline
@torch.inference_mode()
def flux_pipe_call_that_returns_an_iterable_of_images(
    self,
    prompt: Union[str, List[str]] = None,
    prompt_2: Optional[Union[str, List[str]]] = None,
    height: Optional[int] = None,
    width: Optional[int] = None,
    num_inference_steps: int = 28,
    timesteps: List[int] = None,
    guidance_scale: float = 3.5,
    num_images_per_prompt: Optional[int] = 1,
    generator: Optional[Union[torch.Generator, List[torch.Generator]]] = None,
    latents: Optional[torch.FloatTensor] = None,
    prompt_embeds: Optional[torch.FloatTensor] = None,
    pooled_prompt_embeds: Optional[torch.FloatTensor] = None,
    output_type: Optional[str] = "pil",
    return_dict: bool = True,
    joint_attention_kwargs: Optional[Dict[str, Any]] = None,
    max_sequence_length: int = 512,
    good_vae: Optional[Any] = None,
):
    height = height or self.default_sample_size * self.vae_scale_factor
    width = width or self.default_sample_size * self.vae_scale_factor
    
    self.check_inputs(
        prompt,
        prompt_2,
        height,
        width,
        prompt_embeds=prompt_embeds,
        pooled_prompt_embeds=pooled_prompt_embeds,
        max_sequence_length=max_sequence_length,
    )

    self._guidance_scale = guidance_scale
    self._joint_attention_kwargs = joint_attention_kwargs
    self._interrupt = False

    batch_size = 1 if isinstance(prompt, str) else len(prompt)
    device = self._execution_device

    lora_scale = joint_attention_kwargs.get("scale", None) if joint_attention_kwargs is not None else None
    prompt_embeds, pooled_prompt_embeds, text_ids = self.encode_prompt(
        prompt=prompt,
        prompt_2=prompt_2,
        prompt_embeds=prompt_embeds,
        pooled_prompt_embeds=pooled_prompt_embeds,
        device=device,
        num_images_per_prompt=num_images_per_prompt,
        max_sequence_length=max_sequence_length,
        lora_scale=lora_scale,
    )
    
    num_channels_latents = self.transformer.config.in_channels // 4
    latents, latent_image_ids = self.prepare_latents(
        batch_size * num_images_per_prompt,
        num_channels_latents,
        height,
        width,
        prompt_embeds.dtype,
        device,
        generator,
        latents,
    )
    
    sigmas = np.linspace(1.0, 1 / num_inference_steps, num_inference_steps)
    image_seq_len = latents.shape[1]
    mu = calculate_shift(
        image_seq_len,
        self.scheduler.config.base_image_seq_len,
        self.scheduler.config.max_image_seq_len,
        self.scheduler.config.base_shift,
        self.scheduler.config.max_shift,
    )
    timesteps, num_inference_steps = retrieve_timesteps(
        self.scheduler,
        num_inference_steps,
        device,
        timesteps,
        sigmas,
        mu=mu,
    )
    self._num_timesteps = len(timesteps)

    guidance = torch.full([1], guidance_scale, device=device, dtype=torch.float32).expand(latents.shape[0]) if self.transformer.config.guidance_embeds else None

    for i, t in enumerate(timesteps):
        if self.interrupt:
            continue

        timestep = t.expand(latents.shape[0]).to(latents.dtype)

        noise_pred = self.transformer(
            hidden_states=latents,
            timestep=timestep / 1000,
            guidance=guidance,
            pooled_projections=pooled_prompt_embeds,
            encoder_hidden_states=prompt_embeds,
            txt_ids=text_ids,
            img_ids=latent_image_ids,
            joint_attention_kwargs=self.joint_attention_kwargs,
            return_dict=False,
        )[0]

        latents_for_image = self._unpack_latents(latents, height, width, self.vae_scale_factor)
        latents_for_image = (latents_for_image / self.vae.config.scaling_factor) + self.vae.config.shift_factor
        image = self.vae.decode(latents_for_image, return_dict=False)[0]
        yield self.image_processor.postprocess(image, output_type=output_type)[0]
        latents = self.scheduler.step(noise_pred, t, latents, return_dict=False)[0]
        torch.cuda.empty_cache()
        
    latents = self._unpack_latents(latents, height, width, self.vae_scale_factor)
    latents = (latents / good_vae.config.scaling_factor) + good_vae.config.shift_factor
    image = good_vae.decode(latents, return_dict=False)[0]
    self.maybe_free_model_hooks()
    torch.cuda.empty_cache()
    yield self.image_processor.postprocess(image, output_type=output_type)[0]

#-----------------------------------------------------------------------------------LoRA's--------------------------------------------------------------------------#
loras = [
    #1
    {
        "image": "https://huggingface.co/prithivMLmods/Canopus-LoRA-Flux-FaceRealism/resolve/main/images/11.png",
        "title": "Flux Face Realism",
        "repo": "prithivMLmods/Canopus-LoRA-Flux-FaceRealism",
        "trigger_word": "Realism"
    },
    #2
    {
        "image": "https://huggingface.co/alvdansen/softserve_anime/resolve/main/images/ComfyUI_00134_.png",
        "title": "Softserve Anime",
        "repo": "alvdansen/softserve_anime",
        "trigger_word": "sftsrv style illustration"
    },
    #3
    {
        "image": "https://huggingface.co/prithivMLmods/Canopus-LoRA-Flux-Anime/resolve/main/assets/4.png",
        "title": "Flux Anime",
        "repo": "prithivMLmods/Canopus-LoRA-Flux-Anime",
        "trigger_word": "Anime"
    },
    #4
    {
        "image": "https://huggingface.co/Shakker-Labs/FLUX.1-dev-LoRA-One-Click-Creative-Template/resolve/main/images/f2cc649985648e57b9b9b14ca7a8744ac8e50d75b3a334ed4df0f368.jpg",
        "title": "Creative Template",
        "repo": "Shakker-Labs/FLUX.1-dev-LoRA-One-Click-Creative-Template",
        "trigger_word": "The background is 4 real photos, and in the middle is a cartoon picture summarizing the real photos."
    },
    #5
    {
        "image": "https://huggingface.co/prithivMLmods/Canopus-LoRA-Flux-UltraRealism-2.0/resolve/main/images/3.png",
        "title": "Ultra Realism",
        "repo": "prithivMLmods/Canopus-LoRA-Flux-UltraRealism-2.0",
        "trigger_word": "Ultra realistic"
    },
    #6
    {
        "image": "https://huggingface.co/gokaygokay/Flux-Game-Assets-LoRA-v2/resolve/main/images/example_y2bqpuphc.png",
        "title": "Game Assets",
        "repo": "gokaygokay/Flux-Game-Assets-LoRA-v2",
        "trigger_word": "wbgmsst, white background"
    },
    #7
    {
        "image": "https://huggingface.co/alvdansen/softpasty-flux-dev/resolve/main/images/ComfyUI_00814_%20(2).png",
        "title": "Softpasty",
        "repo": "alvdansen/softpasty-flux-dev",
        "trigger_word": "araminta_illus illustration style"
    },
    #8
    {
        "image": "https://huggingface.co/Shakker-Labs/FLUX.1-dev-LoRA-add-details/resolve/main/images/0.png",
        "title": "Details Add",
        "repo": "Shakker-Labs/FLUX.1-dev-LoRA-add-details",
        "trigger_word": ""
    },
    #9
    {
        "image": "https://huggingface.co/alvdansen/frosting_lane_flux/resolve/main/images/content%20-%202024-08-11T010011.238.jpeg",
        "title": "Frosting Lane",
        "repo": "alvdansen/frosting_lane_flux",
        "trigger_word": "frstingln illustration"
    },
    #10
    {
        "image": "https://huggingface.co/aleksa-codes/flux-ghibsky-illustration/resolve/main/images/example5.jpg",
        "title": "Ghibsky Illustration",
        "repo": "aleksa-codes/flux-ghibsky-illustration",
        "trigger_word": "GHIBSKY style painting"
    },
    #11
    {
        "image": "https://huggingface.co/Shakker-Labs/FLUX.1-dev-LoRA-Dark-Fantasy/resolve/main/images/c2215bd73da9f14fcd63cc93350e66e2901bdafa6fb8abaaa2c32a1b.jpg",
        "title": "Dark Fantasy",
        "repo": "Shakker-Labs/FLUX.1-dev-LoRA-Dark-Fantasy",
        "trigger_word": ""
    },
    #12
    {
        "image": "https://huggingface.co/Norod78/Flux_1_Dev_LoRA_Paper-Cutout-Style/resolve/main/d13591878d5043f3989dd6eb1c25b710_233c18effb4b491cb467ca31c97e90b5.png",
        "title": "Paper Cutout",
        "repo": "Norod78/Flux_1_Dev_LoRA_Paper-Cutout-Style",
        "trigger_word": "Paper Cutout Style"
    },
    #13
    {
        "image": "https://huggingface.co/alvdansen/mooniverse/resolve/main/images/out-0%20(17).webp",
        "title": "Mooniverse",
        "repo": "alvdansen/mooniverse",
        "trigger_word": "surreal style"
    },
    #14
    {
        "image": "https://huggingface.co/alvdansen/pola-photo-flux/resolve/main/images/out-0%20-%202024-09-22T130819.351.webp",
        "title": "Pola Photo",
        "repo": "alvdansen/pola-photo-flux",
        "trigger_word": "polaroid style"
    },
    #15
    {
        "image": "https://huggingface.co/multimodalart/flux-tarot-v1/resolve/main/images/7e180627edd846e899b6cd307339140d_5b2a09f0842c476b83b6bd2cb9143a52.png",
        "title": "Flux Tarot",
        "repo": "multimodalart/flux-tarot-v1",
        "trigger_word": "in the style of TOK a trtcrd tarot style"
    },
    #16
    {
        "image": "https://huggingface.co/prithivMLmods/Flux-Dev-Real-Anime-LoRA/resolve/main/images/111.png",
        "title": "Real Anime",
        "repo": "prithivMLmods/Flux-Dev-Real-Anime-LoRA",
        "trigger_word": "Real Anime"
    },
    #17
    {
        "image": "https://huggingface.co/diabolic6045/Flux_Sticker_Lora/resolve/main/images/example_s3pxsewcb.png",
        "title": "Stickers",
        "repo": "diabolic6045/Flux_Sticker_Lora",
        "trigger_word": "5t1cker 5ty1e"
    },
    #18
    {
        "image": "https://huggingface.co/VideoAditor/Flux-Lora-Realism/resolve/main/images/feel-the-difference-between-using-flux-with-lora-from-xlab-v0-j0ehybmvxehd1.png",
        "title": "Realism",
        "repo": "XLabs-AI/flux-RealismLora",
        "trigger_word": ""
    },
    #19
    {
        "image": "https://huggingface.co/alvdansen/flux-koda/resolve/main/images/ComfyUI_00583_%20(1).png",
        "title": "Koda",
        "repo": "alvdansen/flux-koda",
        "trigger_word": "flmft style"
    },
    #20
    {
        "image": "https://huggingface.co/mgwr/Cine-Aesthetic/resolve/main/images/00019-1333633802.png",
        "title": "Cine Aesthetic",
        "repo": "mgwr/Cine-Aesthetic",
        "trigger_word": "mgwr/cine"
    },
    #21
    {
        "image": "https://huggingface.co/SebastianBodza/flux_cute3D/resolve/main/images/astronaut.webp",
        "title": "Cute 3D",
        "repo": "SebastianBodza/flux_cute3D",
        "trigger_word": "NEOCUTE3D"
    },
    #22
    {
        "image": "https://huggingface.co/bingbangboom/flux_dreamscape/resolve/main/images/3.jpg",
        "title": "Dreamscape",
        "repo": "bingbangboom/flux_dreamscape",
        "trigger_word": "in the style of BSstyle004"
    },   
    #23
    {
        "image": "https://huggingface.co/prithivMLmods/Canopus-LoRA-Flux-FaceRealism/resolve/main/images/xc.webp",
        "title": "Cute Kawaii",
        "repo": "prithivMLmods/Canopus-Cute-Kawaii-Flux-LoRA",
        "trigger_word": "cute-kawaii"
    },    
    #24
    {
        "image": "https://cdn-uploads.huggingface.co/production/uploads/64b24543eec33e27dc9a6eca/_jyra-jKP_prXhzxYkg1O.png",
        "title": "Pastel Anime",
        "repo": "Raelina/Flux-Pastel-Anime",
        "trigger_word": "Anime"
    },
    #25
    {
        "image": "https://huggingface.co/Shakker-Labs/FLUX.1-dev-LoRA-Vector-Journey/resolve/main/images/f7a66b51c89896854f31bef743dc30f33c6ea3c0ed8f9ff04d24b702.jpg",
        "title": "Vector",
        "repo": "Shakker-Labs/FLUX.1-dev-LoRA-Vector-Journey",
        "trigger_word": "artistic style blends reality and illustration elements"
    },
    #26
    {
        "image": "https://huggingface.co/bingbangboom/flux-miniature-worlds/resolve/main/images/2.jpg",
        "title": "Miniature",
        "repo": "bingbangboom/flux-miniature-worlds",
        "weights": "flux_MNTRWRLDS.safetensors",
        "trigger_word": "Image in the style of MNTRWRLDS"
    },  
    #27
    {
        "image": "https://huggingface.co/glif-loradex-trainer/bingbangboom_flux_surf/resolve/main/samples/1729012111574__000002000_0.jpg",
        "title": "Surf Bingbangboom",
        "repo": "glif-loradex-trainer/bingbangboom_flux_surf",
        "weights": "flux_surf.safetensors",
        "trigger_word": "SRFNGV01"
    }, 
    #28
    {
        "image": "https://huggingface.co/prithivMLmods/Canopus-Snoopy-Charlie-Brown-Flux-LoRA/resolve/main/000.png",
        "title": "Snoopy Charlie",
        "repo": "prithivMLmods/Canopus-Snoopy-Charlie-Brown-Flux-LoRA",
        "trigger_word": "Snoopy Charlie Brown"
    }, 
    #29
    {
        "image": "https://huggingface.co/alvdansen/sonny-anime-fixed/resolve/main/images/uqAuIMqA6Z7mvPkHg4qJE_f4c3cbe64e0349e7b946d02adeacdca3.png",
        "title": "Fixed Sonny",
        "repo": "alvdansen/sonny-anime-fixed",
        "trigger_word": "nm22 style"
    }, 
    #30
    {
        "image": "https://huggingface.co/davisbro/flux-multi-angle/resolve/main/multi-angle-examples/3.png",
        "title": "Multi Angle",
        "repo": "davisbro/flux-multi-angle",
        "trigger_word": "A TOK composite photo of a person posing at different angles"
    },
    #31
    {
        "image": "https://huggingface.co/glif/how2draw/resolve/main/images/glif-how2draw-araminta-k-vbnvy94npt8m338r2vm02m50.jpg",
        "title": "How2Draw",
        "repo": "glif/how2draw",
        "trigger_word": "How2Draw"
        
    },
    #32
    {
        "image": "https://huggingface.co/Shakker-Labs/FLUX.1-dev-LoRA-Text-Poster/resolve/main/images/6dd1a918d89991ad5e40513ab88e7d892077f89dac93edcf4b660dd2.jpg",
        "title": "Text Poster",
        "repo": "Shakker-Labs/FLUX.1-dev-LoRA-Text-Poster",
        "trigger_word": "text poster"        
    },
    #33
    {
        "image": "https://huggingface.co/SebastianBodza/Flux_Aquarell_Watercolor_v2/resolve/main/images/coffee.webp",
        "title": "Aquarell Watercolor",
        "repo": "SebastianBodza/Flux_Aquarell_Watercolor_v2",
        "trigger_word": "AQUACOLTOK"  
    },
    #34
    {
        "image": "https://huggingface.co/Purz/face-projection/resolve/main/34031797.jpeg",
        "title": "Face Projection ",
        "repo": "Purz/face-projection",
        "trigger_word": "f4c3_p40j3ct10n"  
    },
    #35
    {
        "image": "https://huggingface.co/martintomov/ecom-flux-v2/resolve/main/images/example_z30slf97z.png",
        "title": "Ecom Design Art",
        "repo": "martintomov/ecom-flux-v2",
        "trigger_word": ""
    },
    #36
    {
        "image": "https://huggingface.co/TheAwakenOne/max-headroom/resolve/main/sample/max-headroom_000900_00_20241015234926.png",
        "title": "Max Head-Room",
        "repo": "TheAwakenOne/max-headroom",
        "weights": "max-headroom-v1.safetensors",
        "trigger_word": "M2X, Max-Headroom"
    },
    #37
    {
        "image": "https://huggingface.co/renderartist/toyboxflux/resolve/main/images/3D__00366_.png",
        "title": "Toy Box Flux",
        "repo": "renderartist/toyboxflux",
        "weights": "Toy_Box_Flux_v2_renderartist.safetensors",
        "trigger_word": "t0yb0x, simple toy design, detailed toy design, 3D render"
    },
    #38
    {
        "image": "https://huggingface.co/Shakker-Labs/FLUX.1-dev-LoRA-live-3D/resolve/main/images/51a716fb6fe9ba5d54c260b70e7ff661d38acedc7fb725552fa77bcf.jpg",
        "title": "Live 3D",
        "repo": "Shakker-Labs/FLUX.1-dev-LoRA-live-3D",
        "trigger_word": ""
    },
    #39
    {
        "image": "https://huggingface.co/Shakker-Labs/FLUX.1-dev-LoRA-Garbage-Bag-Art/resolve/main/images/42e944819b43869a03dc252d10409b5944a62494c7082816121016f9.jpg",
        "title": "Garbage Bag Art",
        "repo": "Shakker-Labs/FLUX.1-dev-LoRA-Garbage-Bag-Art",
        "trigger_word": "Inflatable plastic bag"
    },
    #40
    {
        "image": "https://huggingface.co/Shakker-Labs/FLUX.1-dev-LoRA-Logo-Design/resolve/main/images/73e7db6a33550d05836ce285549de60075d05373c7b0660d631dac33.jpg",
        "title": "Logo Design",
        "repo": "Shakker-Labs/FLUX.1-dev-LoRA-Logo-Design",
        "trigger_word": "wablogo, logo, Minimalist"
    }, 
    #41
    {
        "image": "https://huggingface.co/punzel/flux_sadie_sink/resolve/main/images/ComfyUI_Flux_Finetune_00069_.png",
        "title": "Sadie Sink",
        "repo": "punzel/flux_sadie_sink",
        "weights": "flux_sadie_sink.safetensors",
        "trigger_word": "Sadie Sink"
    }, 
    #42
    {
        "image": "https://huggingface.co/punzel/flux_jenna_ortega/resolve/main/images/ComfyUI_Flux_Finetune_00065_.png",
        "title": "Jenna ortega",
        "repo": "punzel/flux_jenna_ortega",
        "weights": "flux_jenna_ortega.safetensors",
        "trigger_word": "Jenna ortega"
    }, 
    #43
    {
        "image": "https://huggingface.co/Wakkamaruh/balatro-poker-cards/resolve/main/samples/01.png",
        "title": "Poker Cards",
        "repo": "Wakkamaruh/balatro-poker-cards",
        "weights": "balatro-poker-cards.safetensors",
        "trigger_word": "balatrocard"
    }, 
    #44
    {
        "image": "https://huggingface.co/lichorosario/flux-cubist-cartoon/resolve/main/samples/albert-einstein.png",
        "title": "Cubist Cartoon",
        "repo": "lichorosario/flux-cubist-cartoon",
        "weights": "lora.safetensors",
        "trigger_word": "CBSTCRTN"
    },
    #45
     {
        "image": "https://huggingface.co/iliketoasters/miniature-people/resolve/main/images/1757-over%20the%20shoulder%20shot%2C%20raw%20photo%2C%20a%20min-fluxcomfy-orgflux1-dev-fp8-128443497-converted.png",
        "title": "Miniature People",
        "repo": "iliketoasters/miniature-people",
        "trigger_word": "miniature people"
    },   
    #46
    {
        "image": "https://huggingface.co/ampp/rough-kids-illustrations/resolve/main/samples/1725115106736__000001000_0.jpg",
        "title": "kids Illustrations",
        "repo": "ampp/rough-kids-illustrations",
        "weights": "rough-kids-illustrations.safetensors",
        "trigger_word": "r0ughkids4rt"  
    },
    #47
    {
        "image": "https://huggingface.co/lichorosario/flux-lora-tstvctr/resolve/main/images/example_mo3jx93o6.png",
        "title": "TSTVCTR Cartoon",
        "repo": "lichorosario/flux-lora-tstvctr",
        "weights": "lora.safetensors",
        "trigger_word": "TSTVCTR cartoon illustration" 
    },
    #48
    {
        "image": "https://huggingface.co/lichorosario/flux-lora-gliff-tosti-vector-no-captions-2500s/resolve/main/images/example_i6h6fi9sq.png",
        "title": "Tosti Vector",
        "repo": "lichorosario/flux-lora-gliff-tosti-vector-no-captions-2500s",
        "weights": "flux_dev_tosti_vector_without_captions_000002500.safetensors",
        "trigger_word": ""     
    },
    #49
    {
        "image": "https://huggingface.co/AlekseyCalvin/Propaganda_Poster_Schnell_by_doctor_diffusion/resolve/main/Trashy.png",
        "title": "Propaganda Poster",
        "repo": "AlekseyCalvin/Propaganda_Poster_Schnell_by_doctor_diffusion",
        "weights": "propaganda_schnell_v1.safetensors",
        "trigger_word": "propaganda poster"          
    },
    #50
    {
        "image": "https://huggingface.co/WizWhite/Wiz-PunchOut_Ringside_Portrait/resolve/main/images/punch0ut__ringside_pixel_portrait_depicting_chris_brown_wearing_a_veil__moonstone_gray_background_with_white_ropes___1923906484.png",
        "title": "Ringside Portrait",
        "repo": "WizWhite/Wiz-PunchOut_Ringside_Portrait",
        "trigger_word": "punch0ut, ringside pixel portrait depicting"     
    },
    #51
    {
        "image": "https://huggingface.co/glif-loradex-trainer/kklors_flux_dev_long_exposure/resolve/main/samples/1729016926778__000003000_3.jpg",
        "title": "Long Exposure",
        "repo": "glif-loradex-trainer/kklors_flux_dev_long_exposure",
        "weights": "flux_dev_long_exposure.safetensors",
        "trigger_word": "LE"     
    },
    #52
    {
        "image": "https://huggingface.co/DamarJati/streetwear-flux/resolve/main/img/79e891f9-ceb8-4f8a-a51d-bb432789d037.jpeg",
        "title": "Street Wear",
        "repo": "DamarJati/streetwear-flux",
        "weights": "Streetwear.safetensors",
        "trigger_word": "Handling Information Tshirt template"      
    },
    #53
    {
        "image": "https://huggingface.co/multimodalart/vintage-ads-flux/resolve/main/samples/-FMpgla6rQ1hBwBpbr-Ao_da7b23c29de14a9cad94901879ae2e2b.png",
        "title": "Vintage Ads Flux",
        "repo": "multimodalart/vintage-ads-flux",
        "weights": "vintage-ads-flux-1350.safetensors",
        "trigger_word": "a vintage ad of" 
    },
    #54
    {
        "image": "https://huggingface.co/multimodalart/product-design/resolve/main/images/example_vgv87rlfl.png",
        "title": "Product Design",
        "repo": "multimodalart/product-design",
        "weights": "product-design.safetensors",
        "trigger_word": "product designed by prdsgn"   
    },
    #55
    {
        "image": "https://huggingface.co/prithivMLmods/Canopus-LoRA-Flux-Typography-ASCII/resolve/main/images/NNN.png",
        "title": "Typography",
        "repo": "prithivMLmods/Canopus-LoRA-Flux-Typography-ASCII",
        "weights": "Typography.safetensors",
        "trigger_word": "Typography, ASCII Art"          
    },
    #56
    {
        "image": "https://huggingface.co/mateo-19182/mosoco/resolve/main/samples/1725714834007__000002000_0.jpg",
        "title": "Mosoco",
        "repo": "mateo-19182/mosoco",
        "weights": "mosoco.safetensors",
        "trigger_word": "moscos0"           
    },
    #57
    {
        "image": "https://huggingface.co/jakedahn/flux-latentpop/resolve/main/images/2.webp",
        "title": "Latent Pop",
        "repo": "jakedahn/flux-latentpop",
        "weights": "lora.safetensors",
        "trigger_word": "latentpop"          
    },
    #58
    {
        "image": "https://huggingface.co/glif-loradex-trainer/ddickinson_dstyl3xl/resolve/main/samples/1728556571974__000001500_2.jpg",
        "title": "Dstyl3xl",
        "repo": "glif-loradex-trainer/ddickinson_dstyl3xl",
        "weights": "dstyl3xl.safetensors",
        "trigger_word": "in the style of dstyl3xl"       
    },
    #59
    {
        "image": "https://huggingface.co/TDN-M/RetouchFLux/resolve/main/images/496f0680-0158-4f37-805d-d227c1a08a7b.png",
        "title": "Retouch FLux",
        "repo": "TDN-M/RetouchFLux",
        "weights": "TDNM_Retouch.safetensors",
        "trigger_word": "luxury, enhance, hdr"        
    },
    #60
    {
        "image": "https://huggingface.co/glif/anime-blockprint-style/resolve/main/images/glif-block-print-anime-flux-dev-araminta-k-lora-araminta-k-e35k8xqsrb8dtq2qcv4gsr3z.jpg",
        "title": "Block Print",
        "repo": "glif/anime-blockprint-style",
        "weights": "bwmanga.safetensors",
        "trigger_word": "blockprint style"              
    },
    #61
    {
        "image": "https://huggingface.co/renderartist/weirdthingsflux/resolve/main/images/3D__02303_.png",
        "title": "Weird Things Flux",
        "repo": "renderartist/weirdthingsflux",
        "weights": "Weird_Things_Flux_v1_renderartist.safetensors",
        "trigger_word": "w3irdth1ngs, illustration"          
    },
    #62
    {
        "image": "https://replicate.delivery/yhqm/z7f2OBcvga07dCoJ4FeRGZCbE5PvipLhogPhEeU7BazIg5lmA/out-0.webp",
        "title": "Replicate Flux LoRA",
        "repo": "lucataco/ReplicateFluxLoRA",
        "weights": "flux_train_replicate.safetensors",
        "trigger_word": "TOK"       
    },
    #63
    {
        "image": "https://cdn-lfs-us-1.hf.co/repos/54/4c/544c698f7773c5b6ada5c775eb35ce2d389bc2420e69e0745ce4d6a22c16223b/6afc4284603ec3f7184dfd4418453fba050800f8b8d620c8b17a36351002c680?response-content-disposition=inline%3B+filename*%3DUTF-8%27%27ComfyUI_00751_.png%3B+filename%3D%22ComfyUI_00751_.png%22%3B&response-content-type=image%2Fpng&Expires=1729841459&Policy=eyJTdGF0ZW1lbnQiOlt7IkNvbmRpdGlvbiI6eyJEYXRlTGVzc1RoYW4iOnsiQVdTOkVwb2NoVGltZSI6MTcyOTg0MTQ1OX19LCJSZXNvdXJjZSI6Imh0dHBzOi8vY2RuLWxmcy11cy0xLmhmLmNvL3JlcG9zLzU0LzRjLzU0NGM2OThmNzc3M2M1YjZhZGE1Yzc3NWViMzVjZTJkMzg5YmMyNDIwZTY5ZTA3NDVjZTRkNmEyMmMxNjIyM2IvNmFmYzQyODQ2MDNlYzNmNzE4NGRmZDQ0MTg0NTNmYmEwNTA4MDBmOGI4ZDYyMGM4YjE3YTM2MzUxMDAyYzY4MD9yZXNwb25zZS1jb250ZW50LWRpc3Bvc2l0aW9uPSomcmVzcG9uc2UtY29udGVudC10eXBlPSoifV19&Signature=u4c4cxEuW7%7EnJ6cSBGi42gcxCnWewSSLamrorwF6NNX5fNZCptHJtC7KOWt8f29v4fu7hDobRVMoyud0-zvaHenw6aGsmkyyKPvX-WfODx3N7UK2sMdj0-vFCY0qFssG2cH1Cpilt7cug9QFpQ3e9W5OQg6onXiwhVJQnf%7ES-Btv-DVeqC-7Wcjz4hyLhYPR03b6Ys7s0N0pI9egZsPJ9XeJkBOw5dw1cp-V21j-ZhjmsoldKKKN19lTFcaK3iogCyZon9nRiOVDAL5FKYf9e2tStbcKkHbTKdHyJWJt1YSw6X3b%7Ef4b2GXdlhMbRuB9RM6B4h1RNYNGwYNjEMhMhA__&Key-Pair-Id=K24J24Z295AEI9",
        "title": "Linework",
        "repo": "alvdansen/haunted_linework_flux",
        "weights": "hauntedlinework_flux_araminta_k.safetensors",
        "trigger_word": "hntdlnwrk style"         
    },
    #64
    {
        "image": "https://huggingface.co/fofr/flux-cassette-futurism/resolve/main/images/example_qgry9jnkj.png",
        "title": "Cassette Futurism",
        "repo": "fofr/flux-cassette-futurism",
        "weights": "lora.safetensors",
        "trigger_word": "cassette futurism"   
    },
    #65
    {
        "image": "https://huggingface.co/Wadaka/Mojo_Style_LoRA/resolve/main/Samples/Sample2.png",
        "title": "Mojo Style",
        "repo": "Wadaka/Mojo_Style_LoRA",
        "weights": "Mojo_Style_LoRA.safetensors",
        "trigger_word": "Mojo_Style" 
        
    },
    #66
    {
        "image": "https://huggingface.co/Norod78/JojosoStyle-flux-lora/resolve/main/samples/1725244218477__000004255_1.jpg",
        "title": "Jojoso Style",
        "repo": "Norod78/JojosoStyle-flux-lora",
        "weights": "JojosoStyle_flux_lora.safetensors",
        "trigger_word": "JojosoStyle"        
    },
    #67
    {
        "image": "https://huggingface.co/Chunte/flux-lora-Huggieverse/resolve/main/images/Happy%20star.png",
        "title": "Huggieverse",
        "repo": "Chunte/flux-lora-Huggieverse",
        "weights": "lora.safetensors",
        "trigger_word": "HGGRE"          
    },
    #68
    {
        "image": "https://huggingface.co/diabolic6045/Flux_Wallpaper_Lora/resolve/main/images/example_hjp51et93.png",
        "title": "Wallpaper LoRA",
        "repo": "diabolic6045/Flux_Wallpaper_Lora",
        "weights": "tost-2024-09-20-07-35-44-wallpap3r5.safetensors",
        "trigger_word": "wallpap3r5"        
    },
    #69
    {
        "image": "https://huggingface.co/bingbangboom/flux_geopop/resolve/main/extras/5.png",
        "title": "Geo Pop",
        "repo": "bingbangboom/flux_geopop",
        "weights": "geopop_NWGMTRCPOPV01.safetensors",
        "trigger_word": "illustration in the style of NWGMTRCPOPV01"       
    },
    #70
    {
        "image": "https://huggingface.co/bingbangboom/flux_colorscape/resolve/main/images/4.jpg",
        "title": "Colorscape",
        "repo": "bingbangboom/flux_colorscape",
        "weights": "flux_colorscape.safetensors",
        "trigger_word": "illustration in the style of ASstyle001" 
    },
    #71
    {
        "image": "https://huggingface.co/dvyio/flux-lora-thermal-image/resolve/main/images/WROSaNNU4-Gw0r5QoBRjf_f164ffa4f0804e68bad1d06d30deecfa.jpg",
        "title": "Thermal Image",
        "repo": "dvyio/flux-lora-thermal-image",
        "weights": "79b5004c57ef4c4390dead1c65977bbb_pytorch_lora_weights.safetensors",
        "trigger_word": "thermal image in the style of THRML" 
    },
    #72
    {
        "image": "https://huggingface.co/prithivMLmods/Canopus-Clothing-Flux-LoRA/resolve/main/images/333.png",
        "title": "Clothing Flux",
        "repo": "prithivMLmods/Canopus-Clothing-Flux-LoRA",
        "weights": "Canopus-Clothing-Flux-Dev-Florence2-LoRA.safetensors",
        "trigger_word": "Hoodie, Clothes, Shirt, Pant"    
    },
    #73
    {
        "image": "https://huggingface.co/dvyio/flux-lora-stippled-illustration/resolve/main/images/57FPpbu74QTV45w6oNOtZ_26832270585f456c99e4a98b1c073745.jpg",
        "title": "Stippled Illustration",
        "repo": "dvyio/flux-lora-stippled-illustration",
        "weights": "31984be602a04a1fa296d9ccb244fb29_pytorch_lora_weights.safetensors",
        "trigger_word": "stippled illustration in the style of STPPLD"          
    },
    #74
    {
        "image": "https://huggingface.co/wayned/fruitlabels/resolve/main/images/ComfyUI_03969_.png",
        "title": "Fruitlabels",
        "repo": "wayned/fruitlabels",
        "weights": "fruitlabels2.safetensors",
        "trigger_word": "fruit labels"
        
    },
    #75
    {
        "image": "https://huggingface.co/punzel/flux_margot_robbie/resolve/main/images/ComfyUI_Flux_Finetune_00142_.png",
        "title": "Margot Robbie",
        "repo": "punzel/flux_margot_robbie",
        "weights": "flux_margot_robbie.safetensors",
        "trigger_word": ""
    },
    #76
    {
        "image": "https://huggingface.co/diabolic6045/Formula1_Lego_Lora/resolve/main/images/example_502kcuiba.png",
        "title": "Formula 1 Lego",
        "repo": "punzel/flux_margot_robbie",
        "weights": "tost-2024-09-20-09-58-33-f1leg0s.safetensors",
        "trigger_word": "f1leg0s"    
    },
    #77
    {
        "image": "https://huggingface.co/glif/Brain-Melt-Acid-Art/resolve/main/images/IMG_0832.png",
        "title": "Melt Acid",
        "repo": "glif/Brain-Melt-Acid-Art",
        "weights": "Brain_Melt.safetensors",
        "trigger_word": "in an acid surrealism style, maximalism"  
    },
    #78
    {
        "image": "https://huggingface.co/jeremytai/enso-zen/resolve/main/images/example_a0iwdj5lu.png",
        "title": "Enso",
        "repo": "jeremytai/enso-zen",
        "weights": "enso-zen.safetensors",
        "trigger_word": "enso"       
    },
    #79
    {
        "image": "https://huggingface.co/veryVANYA/opus-ascii-flux/resolve/main/31654332.jpeg",
        "title": "Opus Ascii",
        "repo": "veryVANYA/opus-ascii-flux",
        "weights": "flux_opus_ascii.safetensors",
        "trigger_word": "opus_ascii" 
    },
    #80
    {
        "image": "https://huggingface.co/crystantine/cybrpnkz/resolve/main/images/example_plyxk0lej.png",
        "title": "Cybrpnkz",
        "repo": "crystantine/cybrpnkz",
        "weights": "cybrpnkz.safetensors",
        "trigger_word": "architecture style of CYBRPNKZ"
    },
    #81
    {
        "image": "https://huggingface.co/fyp1/pattern_generation/resolve/main/images/1727560066052__000001000_7.jpg",
        "title": "Pattern Generation",
        "repo": "fyp1/pattern_generation",
        "weights": "flux_dev_finetune.safetensors",
        "trigger_word": "pattern"
    },
    #82
    {
        "image": "https://huggingface.co/TheAwakenOne/caricature/resolve/main/sample/caricature_000900_03_20241007143412.png",
        "title": "Caricature",
        "repo": "TheAwakenOne/caricature",
        "weights": "caricature.safetensors",
        "trigger_word": "CCTUR3"      
    },
    #83
    {
        "image": "https://huggingface.co/davidrd123/Flux-MoonLanding76-Replicate/resolve/main/images/example_6adktoq5m.png",
        "title": "MoonLanding 76",
        "repo": "davidrd123/Flux-MoonLanding76-Replicate",
        "weights": "lora.safetensors",
        "trigger_word": "m00nl4nd1ng"          
    },
    #84
    {
        "image": "https://huggingface.co/Purz/neon-sign/resolve/main/33944768.jpeg",
        "title": "Neon",
        "repo": "Purz/neon-sign",
        "weights": "purz-n30n_51gn.safetensors",
        "trigger_word": "n30n_51gn"        
    },
    #85
    {
        "image": "https://huggingface.co/WizWhite/wizard-s-vintage-sardine-tins/resolve/main/27597694.jpeg",
        "title": "Vintage Sardine Tins",
        "repo": "WizWhite/wizard-s-vintage-sardine-tins",
        "weights": "Wiz-SardineTins_Flux.safetensors",
        "trigger_word": "Vintage Sardine Tin, Tinned Fish, vintage xyz tin"    
    },
    #86
    {
        "image": "https://huggingface.co/TheAwakenOne/mtdp-balloon-character/resolve/main/sample/mtdp-balloon-character_000200_01_20241014221110.png",
        "title": "Float Ballon Character",
        "repo": "TheAwakenOne/mtdp-balloon-character",
        "weights": "mtdp-balloon-character.safetensors",
        "trigger_word": "FLOAT"   
    },
    #87
    {
        "image": "https://huggingface.co/glif/golden-haggadah/resolve/main/images/6aca6403-ecd6-4216-a66a-490ae25ff1b2.jpg",
        "title": "Golden Haggadah",
        "repo": "glif/golden-haggadah",
        "weights": "golden_haggadah.safetensors",
        "trigger_word": "golden haggadah style"        
    },
    #88
    {
        "image": "https://huggingface.co/glif-loradex-trainer/usernametaken420__oz_ftw_balaclava/resolve/main/samples/1729278631255__000001500_1.jpg",
        "title": "Ftw Balaclava",
        "repo": "glif-loradex-trainer/usernametaken420__oz_ftw_balaclava",
        "weights": "oz_ftw_balaclava.safetensors",
        "trigger_word": "ftw balaclava"          
    },
    #89
    {
        "image": "https://huggingface.co/AlloReview/flux-lora-undraw/resolve/main/images/Flux%20Lora%20Undraw%20Prediction.webp",
        "title": "Undraw",
        "repo": "AlloReview/flux-lora-undraw",
        "weights": "lora.safetensors",
        "trigger_word": "in the style of UndrawPurple"      
    },
    #90
    {
        "image": "https://huggingface.co/Disra/lora-anime-test-02/resolve/main/assets/image_0_0.png",
        "title": "Anime Test",
        "repo": "Disra/lora-anime-test-02",
        "weights": "pytorch_lora_weights.safetensors",
        "trigger_word": "anime" 
    },
    #91
    {
        "image": "https://huggingface.co/wanghaofan/Black-Myth-Wukong-FLUX-LoRA/resolve/main/images/7d0ac495a4d5e4a3a30df25f08379a3f956ef99e1dc3e252fc1fca3a.jpg",
        "title": "Black Myth Wukong",
        "repo": "wanghaofan/Black-Myth-Wukong-FLUX-LoRA",
        "weights": "pytorch_lora_weights.safetensors",
        "trigger_word": "wukong"         
    },
    #92
    {
        "image": "https://huggingface.co/nerijs/pastelcomic-flux/resolve/main/images/4uZ_vaYg-HQnfa5D9gfli_38bf3f95d8b345e5a9bd42d978a15267.png",
        "title": "Pastelcomic",
        "repo": "nerijs/pastelcomic-flux",
        "weights": "pastelcomic_v1.safetensors",
        "trigger_word": ""            
    },
    #93
    {
        "image": "https://huggingface.co/RareConcepts/Flux.1-dev-LoKr-Moonman/resolve/main/assets/image_6_0.png",
        "title": "Moonman",
        "repo": "RareConcepts/Flux.1-dev-LoKr-Moonman",
        "weights": "pytorch_lora_weights.safetensors",
        "trigger_word": "moonman"            
    },
    #94
    {
        "image": "https://huggingface.co/martintomov/ascii-flux-v1/resolve/main/images/0af53645-ddcc-4803-93c8-f7e43f6fbbd1.jpeg",
        "title": "Ascii Flux",
        "repo": "martintomov/ascii-flux-v1",
        "weights": "ascii-art-v1.safetensors",
        "trigger_word": "ASCII art"          
    },
    #95
    {
        "image": "https://huggingface.co/Omarito2412/Stars-Galaxy-Flux/resolve/main/images/25128409.jpeg",
        "title": "Ascii Flux",
        "repo": "Omarito2412/Stars-Galaxy-Flux",
        "weights": "Stars_Galaxy_Flux.safetensors",
        "trigger_word": "mlkwglx"       
    },
    #96
    {
        "image": "https://huggingface.co/brushpenbob/flux-pencil-v2/resolve/main/26193927.jpeg",
        "title": "Pencil V2",
        "repo": "brushpenbob/flux-pencil-v2",
        "weights": "Flux_Pencil_v2_r1.safetensors",
        "trigger_word": "evang style"           
    },
    #97
    {
        "image": "https://huggingface.co/Shakker-Labs/FLUX.1-dev-LoRA-Children-Simple-Sketch/resolve/main/images/1f20519208cef367af2fda8d91ddbba674f39b097389d12ee25b4cb1.jpg",
        "title": "Children Simple Sketch",
        "repo": "Shakker-Labs/FLUX.1-dev-LoRA-Children-Simple-Sketch",
        "weights": "FLUX-dev-lora-children-simple-sketch.safetensors",
        "trigger_word": "sketched style"            
    },
    #98
    {
        "image": "https://huggingface.co/victor/contemporarink/resolve/main/images/example_hnqc22urm.png",
        "title": "Contemporarink",
        "repo": "victor/contemporarink",
        "weights": "inky-colors.safetensors",
        "trigger_word": "ECACX"         
    },
    #99
    {
        "image": "https://huggingface.co/wavymulder/OverlordStyleFLUX/resolve/main/imgs/ComfyUI_00668_.png",
        "title": "OverlordStyle",
        "repo": "wavymulder/OverlordStyleFLUX",
        "weights": "ovld_style_overlord_wavymulder.safetensors",
        "trigger_word": "ovld style anime"          
    },
    #100
    {
        "image": "https://huggingface.co/wavymulder/OverlordStyleFLUX/resolve/main/imgs/ComfyUI_00668_.png",
        "title": "Canny quest",
        "repo": "marceloxp/canny-quest",
        "weights": "Canny_Quest-000004.safetensors",
        "trigger_word": "blonde, silver silk dress, perfectly round sunglasses, pearl necklace"         
    },
    #101
    {
        "image": "https://huggingface.co/busetolunay/building_flux_lora_v1/resolve/main/samples/1725469125185__000001250_2.jpg",
        "title": "Building Flux",
        "repo": "busetolunay/building_flux_lora_v1",
        "weights": "building_flux_lora_v4.safetensors",
        "trigger_word": "a0ce"        
    },
    #102
    {
        "image": "https://huggingface.co/Omarito2412/Tinker-Bell-Flux/resolve/main/images/9e9e7eda-3ddf-467a-a7f8-6d8e3ef80cd0.png",
        "title": "Tinker Bell Flux",
        "repo": "Omarito2412/Tinker-Bell-Flux",
        "weights": "TinkerBellV2-FLUX.safetensors",
        "trigger_word": "TinkerWaifu, blue eyes, single hair bun"        
    },
    #103
    {
        "image": "https://huggingface.co/Shakker-Labs/FLUX.1-dev-LoRA-playful-metropolis/resolve/main/images/3e9265312b3b726c224a955ec9254a0f95c2c8b78ce635929183a075.jpg",
        "title": "Playful Metropolis",
        "repo": "Shakker-Labs/FLUX.1-dev-LoRA-playful-metropolis",
        "weights": "FLUX-dev-lora-playful_metropolis.safetensors",
        "trigger_word": ""        
    }
    #add new
]

#--------------------------------------------------Model Initialization-----------------------------------------------------------------------------------------#

dtype = torch.bfloat16
device = "cuda" if torch.cuda.is_available() else "cpu"
base_model = "black-forest-labs/FLUX.1-dev"

#TAEF1 is very tiny autoencoder which uses the same "latent API" as FLUX.1's VAE. FLUX.1 is useful for real-time previewing of the FLUX.1 generation process.#
taef1 = AutoencoderTiny.from_pretrained("madebyollin/taef1", torch_dtype=dtype).to(device)
good_vae = AutoencoderKL.from_pretrained(base_model, subfolder="vae", torch_dtype=dtype).to(device)
pipe = DiffusionPipeline.from_pretrained(base_model, torch_dtype=dtype, vae=taef1).to(device)
pipe_i2i = AutoPipelineForImage2Image.from_pretrained(base_model,
                                                      vae=good_vae,
                                                      transformer=pipe.transformer,
                                                      text_encoder=pipe.text_encoder,
                                                      tokenizer=pipe.tokenizer,
                                                      text_encoder_2=pipe.text_encoder_2,
                                                      tokenizer_2=pipe.tokenizer_2,
                                                      torch_dtype=dtype
                                                     )

MAX_SEED = 2**32-1

pipe.flux_pipe_call_that_returns_an_iterable_of_images = flux_pipe_call_that_returns_an_iterable_of_images.__get__(pipe)

class calculateDuration:
    def __init__(self, activity_name=""):
        self.activity_name = activity_name

    def __enter__(self):
        self.start_time = time.time()
        return self
    
    def __exit__(self, exc_type, exc_value, traceback):
        self.end_time = time.time()
        self.elapsed_time = self.end_time - self.start_time
        if self.activity_name:
            print(f"Elapsed time for {self.activity_name}: {self.elapsed_time:.6f} seconds")
        else:
            print(f"Elapsed time: {self.elapsed_time:.6f} seconds")

def update_selection(evt: gr.SelectData, width, height):
    selected_lora = loras[evt.index]
    new_placeholder = f"Type a prompt for {selected_lora['title']}"
    lora_repo = selected_lora["repo"]
    updated_text = f"### Selected: [{lora_repo}](https://huggingface.co/{lora_repo}) ✅"
    if "aspect" in selected_lora:
        if selected_lora["aspect"] == "portrait":
            width = 768
            height = 1024
        elif selected_lora["aspect"] == "landscape":
            width = 1024
            height = 768
        else:
            width = 1024
            height = 1024
    return (
        gr.update(placeholder=new_placeholder),
        updated_text,
        evt.index,
        width,
        height,
    )

@spaces.GPU(duration=100)
def generate_image(prompt_mash, steps, seed, cfg_scale, width, height, lora_scale, progress):
    pipe.to("cuda")
    generator = torch.Generator(device="cuda").manual_seed(seed)
    with calculateDuration("Generating image"):
        # Generate image
        for img in pipe.flux_pipe_call_that_returns_an_iterable_of_images(
            prompt=prompt_mash,
            num_inference_steps=steps,
            guidance_scale=cfg_scale,
            width=width,
            height=height,
            generator=generator,
            joint_attention_kwargs={"scale": lora_scale},
            output_type="pil",
            good_vae=good_vae,
        ):
            yield img

def generate_image_to_image(prompt_mash, image_input_path, image_strength, steps, cfg_scale, width, height, lora_scale, seed):
    generator = torch.Generator(device="cuda").manual_seed(seed)
    pipe_i2i.to("cuda")
    image_input = load_image(image_input_path)
    final_image = pipe_i2i(
        prompt=prompt_mash,
        image=image_input,
        strength=image_strength,
        num_inference_steps=steps,
        guidance_scale=cfg_scale,
        width=width,
        height=height,
        generator=generator,
        joint_attention_kwargs={"scale": lora_scale},
        output_type="pil",
    ).images[0]
    return final_image 

@spaces.GPU(duration=100)
def run_lora(prompt, image_input, image_strength, cfg_scale, steps, selected_index, randomize_seed, seed, width, height, lora_scale, progress=gr.Progress(track_tqdm=True)):
    if selected_index is None:
        raise gr.Error("You must select a LoRA before proceeding.")
    selected_lora = loras[selected_index]
    lora_path = selected_lora["repo"]
    trigger_word = selected_lora["trigger_word"]
    if(trigger_word):
        if "trigger_position" in selected_lora:
            if selected_lora["trigger_position"] == "prepend":
                prompt_mash = f"{trigger_word} {prompt}"
            else:
                prompt_mash = f"{prompt} {trigger_word}"
        else:
            prompt_mash = f"{trigger_word} {prompt}"
    else:
        prompt_mash = prompt

    with calculateDuration("Unloading LoRA"):
        pipe.unload_lora_weights()
        pipe_i2i.unload_lora_weights()
        
    #LoRA weights flow
    with calculateDuration(f"Loading LoRA weights for {selected_lora['title']}"):
        pipe_to_use = pipe_i2i if image_input is not None else pipe
        weight_name = selected_lora.get("weights", None)
        
        pipe_to_use.load_lora_weights(
            lora_path, 
            weight_name=weight_name, 
            low_cpu_mem_usage=True
        )
            
    with calculateDuration("Randomizing seed"):
        if randomize_seed:
            seed = random.randint(0, MAX_SEED)
            
    if(image_input is not None):
        
        final_image = generate_image_to_image(prompt_mash, image_input, image_strength, steps, cfg_scale, width, height, lora_scale, seed)
        yield final_image, seed, gr.update(visible=False)
    else:
        image_generator = generate_image(prompt_mash, steps, seed, cfg_scale, width, height, lora_scale, progress)
    
        final_image = None
        step_counter = 0
        for image in image_generator:
            step_counter+=1
            final_image = image
            progress_bar = f'<div class="progress-container"><div class="progress-bar" style="--current: {step_counter}; --total: {steps};"></div></div>'
            yield image, seed, gr.update(value=progress_bar, visible=True)
            
        yield final_image, seed, gr.update(value=progress_bar, visible=False)
        
def get_huggingface_safetensors(link):
  split_link = link.split("/")
  if(len(split_link) == 2):
            model_card = ModelCard.load(link)
            base_model = model_card.data.get("base_model")
            print(base_model)
      
            #Allows Both
            if((base_model != "black-forest-labs/FLUX.1-dev") and (base_model != "black-forest-labs/FLUX.1-schnell")):
                raise Exception("Flux LoRA Not Found!")
                
            # Only allow "black-forest-labs/FLUX.1-dev"
            #if base_model != "black-forest-labs/FLUX.1-dev":
                #raise Exception("Only FLUX.1-dev is supported, other LoRA models are not allowed!")
                
            image_path = model_card.data.get("widget", [{}])[0].get("output", {}).get("url", None)
            trigger_word = model_card.data.get("instance_prompt", "")
            image_url = f"https://huggingface.co/{link}/resolve/main/{image_path}" if image_path else None
            fs = HfFileSystem()
            try:
                list_of_files = fs.ls(link, detail=False)
                for file in list_of_files:
                    if(file.endswith(".safetensors")):
                        safetensors_name = file.split("/")[-1]
                    if (not image_url and file.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))):
                      image_elements = file.split("/")
                      image_url = f"https://huggingface.co/{link}/resolve/main/{image_elements[-1]}"
            except Exception as e:
              print(e)
              gr.Warning(f"You didn't include a link neither a valid Hugging Face repository with a *.safetensors LoRA")
              raise Exception(f"You didn't include a link neither a valid Hugging Face repository with a *.safetensors LoRA")
            return split_link[1], link, safetensors_name, trigger_word, image_url

def check_custom_model(link):
    if(link.startswith("https://")):
        if(link.startswith("https://huggingface.co") or link.startswith("https://www.huggingface.co")):
            link_split = link.split("huggingface.co/")
            return get_huggingface_safetensors(link_split[1])
    else: 
        return get_huggingface_safetensors(link)

def add_custom_lora(custom_lora):
    global loras
    if(custom_lora):
        try:
            title, repo, path, trigger_word, image = check_custom_model(custom_lora)
            print(f"Loaded custom LoRA: {repo}")
            card = f'''
            <div class="custom_lora_card">
              <span>Loaded custom LoRA:</span>
              <div class="card_internal">
                <img src="{image}" />
                <div>
                    <h3>{title}</h3>
                    <small>{"Using: <code><b>"+trigger_word+"</code></b> as the trigger word" if trigger_word else "No trigger word found. If there's a trigger word, include it in your prompt"}<br></small>
                </div>
              </div>
            </div>
            '''
            existing_item_index = next((index for (index, item) in enumerate(loras) if item['repo'] == repo), None)
            if(not existing_item_index):
                new_item = {
                    "image": image,
                    "title": title,
                    "repo": repo,
                    "weights": path,
                    "trigger_word": trigger_word
                }
                print(new_item)
                existing_item_index = len(loras)
                loras.append(new_item)
        
            return gr.update(visible=True, value=card), gr.update(visible=True), gr.Gallery(selected_index=None), f"Custom: {path}", existing_item_index, trigger_word
        except Exception as e:
            gr.Warning(f"Invalid LoRA: either you entered an invalid link, or a non-FLUX LoRA")
            return gr.update(visible=True, value=f"Invalid LoRA: either you entered an invalid link, a non-FLUX LoRA"), gr.update(visible=False), gr.update(), "", None, ""
    else:
        return gr.update(visible=False), gr.update(visible=False), gr.update(), "", None, ""

def remove_custom_lora():
    return gr.update(visible=False), gr.update(visible=False), gr.update(), "", None, ""

run_lora.zerogpu = True

css = '''
#gen_btn{height: 100%}
#gen_column{align-self: stretch}
#title{text-align: center}
#title h1{font-size: 3em; display:inline-flex; align-items:center}
#title img{width: 100px; margin-right: 0.5em}
#gallery .grid-wrap{height: 10vh}
#lora_list{background: var(--block-background-fill);padding: 0 1em .3em; font-size: 90%}
.card_internal{display: flex;height: 100px;margin-top: .5em}
.card_internal img{margin-right: 1em}
.styler{--form-gap-width: 0px !important}
#progress{height:30px}
#progress .generating{display:none}
.progress-container {width: 100%;height: 30px;background-color: #f0f0f0;border-radius: 15px;overflow: hidden;margin-bottom: 20px}
.progress-bar {height: 100%;background-color: #4f46e5;width: calc(var(--current) / var(--total) * 100%);transition: width 0.5s ease-in-out}
'''

with gr.Blocks(theme="prithivMLmods/Minecraft-Theme", css=css, delete_cache=(60, 60)) as app:
    title = gr.HTML(
        """<h1>FLUX LoRA DLC🥳</h1>""",
        elem_id="title",
    )
    selected_index = gr.State(None)
    with gr.Row():
        with gr.Column(scale=3):
            prompt = gr.Textbox(label="Prompt", lines=1, placeholder="Choose the LoRA and type the prompt")
        with gr.Column(scale=1, elem_id="gen_column"):
            generate_button = gr.Button("Generate", variant="primary", elem_id="gen_btn")
    with gr.Row():
        with gr.Column():
            selected_info = gr.Markdown("")
            gallery = gr.Gallery(
                [(item["image"], item["title"]) for item in loras],
                label="LoRA DLC's",
                allow_preview=False,
                columns=3,
                elem_id="gallery",
                show_share_button=False
            )
            with gr.Group():
                custom_lora = gr.Textbox(label="Enter Custom LoRA", placeholder="prithivMLmods/Canopus-LoRA-Flux-Anime")
                gr.Markdown("[Check the list of FLUX LoRA's](https://huggingface.co/models?other=base_model:adapter:black-forest-labs/FLUX.1-dev)", elem_id="lora_list")
            custom_lora_info = gr.HTML(visible=False)
            custom_lora_button = gr.Button("Remove custom LoRA", visible=False)
        with gr.Column():
            progress_bar = gr.Markdown(elem_id="progress",visible=False)
            result = gr.Image(label="Generated Image")

    with gr.Row():
        with gr.Accordion("Advanced Settings", open=False):
            with gr.Row():
                input_image = gr.Image(label="Input image", type="filepath")
                image_strength = gr.Slider(label="Denoise Strength", info="Lower means more image influence", minimum=0.1, maximum=1.0, step=0.01, value=0.75)
            with gr.Column():
                with gr.Row():
                    cfg_scale = gr.Slider(label="CFG Scale", minimum=1, maximum=20, step=0.5, value=3.5)
                    steps = gr.Slider(label="Steps", minimum=1, maximum=50, step=1, value=28)
                
                with gr.Row():
                    width = gr.Slider(label="Width", minimum=256, maximum=1536, step=64, value=1024)
                    height = gr.Slider(label="Height", minimum=256, maximum=1536, step=64, value=1024)
                
                with gr.Row():
                    randomize_seed = gr.Checkbox(True, label="Randomize seed")
                    seed = gr.Slider(label="Seed", minimum=0, maximum=MAX_SEED, step=1, value=0, randomize=True)
                    lora_scale = gr.Slider(label="LoRA Scale", minimum=0, maximum=3, step=0.01, value=0.95)

    gallery.select(
        update_selection,
        inputs=[width, height],
        outputs=[prompt, selected_info, selected_index, width, height]
    )
    custom_lora.input(
        add_custom_lora,
        inputs=[custom_lora],
        outputs=[custom_lora_info, custom_lora_button, gallery, selected_info, selected_index, prompt]
    )
    custom_lora_button.click(
        remove_custom_lora,
        outputs=[custom_lora_info, custom_lora_button, gallery, selected_info, selected_index, custom_lora]
    )
    gr.on(
        triggers=[generate_button.click, prompt.submit],
        fn=run_lora,
        inputs=[prompt, input_image, image_strength, cfg_scale, steps, selected_index, randomize_seed, seed, width, height, lora_scale],
        outputs=[result, seed, progress_bar]
    )

app.queue()
app.launch()