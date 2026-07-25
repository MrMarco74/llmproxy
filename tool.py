"""
title: ComfyUI Model Switcher
author: mrmarco
description: Switch the active ComfyUI image generation model and LoRAs
version: 4.0.0
"""
import json
import sqlite3
from pydantic import BaseModel

MODEL_MAP = {
    "juggernaut":   "juggernautXL_v9Rdphoto2Lightning.safetensors",
    "pony":         "ponyDiffusionV6XL.safetensors",
    "dreamshaper":  "dreamshaperXL_turbo.safetensors",
    "realvis":      "realvisxlV50_v50Bakedvae.safetensors",
    "anime":        "wai_illustrious_xl.safetensors",
    "nova":         "nova_anime_xl.safetensors",
    "comic":        "ponyDiffusionV6XL.safetensors",
}

LORA_STACKS = {
    "pony": [
        ("detail_tweaker_xl.safetensors", 0.8, 0.8),
        ("nudify_xl_better_bodies.safetensors", 0.6, 0.6),
        ("hands_xl_fix.safetensors", 0.5, 0.5),
    ],
    "juggernaut": [
        ("detail_tweaker_xl.safetensors", 0.6, 0.6),
        ("nudify_xl_better_bodies.safetensors", 0.5, 0.5),
        ("hands_xl_fix.safetensors", 0.5, 0.5),
    ],
    "realvis": [
        ("detail_tweaker_xl.safetensors", 0.7, 0.7),
        ("nudify_xl_better_bodies.safetensors", 0.6, 0.6),
        ("labiaplasty_innie_pussy_xl.safetensors", 0.7, 0.7),
        ("pov_missionary_vaginal.safetensors", 0.6, 0.6),
        ("nsfw_pov_all_in_one_mini.safetensors", 0.5, 0.5),
        ("hands_xl_fix.safetensors", 0.5, 0.5),
    ],
    "dreamshaper": [
        ("add_detail_xl.safetensors", 0.7, 0.7),
        ("hands_xl_fix.safetensors", 0.5, 0.5),
    ],
    "anime": [
        ("expressive_h_hentai_xl.safetensors", 0.7, 0.7),
        ("nudify_xl_better_bodies.safetensors", 0.5, 0.5),
        ("hands_xl_fix.safetensors", 0.5, 0.5),
    ],
    "nova": [
        ("expressive_h_hentai_xl.safetensors", 0.65, 0.65),
        ("nudify_xl_better_bodies.safetensors", 0.5, 0.5),
        ("hands_xl_fix.safetensors", 0.5, 0.5),
        ("anime_lineart_manga.safetensors", 0.3, 0.3),
    ],
    "comic": [
        ("incase_style_pony.safetensors", 0.85, 0.85),
        ("nudify_xl_better_bodies.safetensors", 0.5, 0.5),
        ("hands_xl_fix.safetensors", 0.4, 0.4),
    ],
}

SAMPLER_SETTINGS = {
    "pony":        {"cfg": 4.5, "steps": 25, "sampler_name": "euler_ancestral", "scheduler": "karras"},
    "juggernaut":  {"cfg": 4.5, "steps": 25, "sampler_name": "euler_ancestral", "scheduler": "karras"},
    "realvis":     {"cfg": 5.0, "steps": 25, "sampler_name": "dpmpp_2m",        "scheduler": "karras"},
    "dreamshaper": {"cfg": 6.0, "steps": 20, "sampler_name": "dpmpp_sde",       "scheduler": "karras"},
    "anime":       {"cfg": 5.0, "steps": 28, "sampler_name": "euler_ancestral", "scheduler": "karras"},
    "nova":        {"cfg": 5.0, "steps": 28, "sampler_name": "euler_ancestral", "scheduler": "karras"},
    "comic":       {"cfg": 5.0, "steps": 25, "sampler_name": "euler_ancestral", "scheduler": "karras"},
}

NEGATIVE_PROMPTS = {
    "pony":    "score_4, score_5, score_6, bad anatomy, extra limbs, deformed, ugly, bad hands, watermark, censored, mosaic, blurry, low quality, worst quality",
    "anime":   "score_4, score_5, score_6, bad anatomy, extra limbs, deformed, bad hands, watermark, censored, mosaic, blurry, worst quality, lowres",
    "nova":    "score_4, score_5, score_6, bad anatomy, extra limbs, deformed, bad hands, watermark, censored, mosaic, blurry, worst quality, lowres",
    "comic":   "score_4, score_5, score_6, bad anatomy, extra limbs, deformed, bad hands, watermark, censored, mosaic, blurry, worst quality, photorealistic",
    "default": "bad anatomy, extra limbs, deformed, ugly, bad hands, watermark, censored, mosaic, blurry, low quality, worst quality, jpeg artifacts",
}

DB_PATH = "/app/backend/data/webui.db"


def _build_workflow_with_loras(base_model: str, model_key: str) -> str:
    loras = LORA_STACKS.get(model_key, [])
    sampler = SAMPLER_SETTINGS.get(model_key, {"cfg": 4.5, "steps": 25, "sampler_name": "euler_ancestral", "scheduler": "karras"})
    neg_text = NEGATIVE_PROMPTS.get(model_key, NEGATIVE_PROMPTS["default"])

    wf = {
        "1": {"inputs": {"vae_name": "sdxl_vae_fp16fix.safetensors"}, "class_type": "VAELoader"},
        "4": {"inputs": {"ckpt_name": base_model}, "class_type": "CheckpointLoaderSimple"},
    }

    lora_node_start = 20
    prev_model = ["4", 0]
    prev_clip = ["4", 1]

    for i, (lname, ms, cs) in enumerate(loras):
        nid = str(lora_node_start + i)
        wf[nid] = {
            "class_type": "LoraLoader",
            "inputs": {"model": prev_model, "clip": prev_clip, "lora_name": lname, "strength_model": ms, "strength_clip": cs}
        }
        prev_model = [nid, 0]
        prev_clip = [nid, 1]

    wf.update({
        "5":  {"inputs": {"width": 1024, "height": 1024, "batch_size": 1}, "class_type": "EmptyLatentImage"},
        "6":  {"inputs": {"text": "Prompt", "clip": prev_clip}, "class_type": "CLIPTextEncode"},
        "7":  {"inputs": {"text": neg_text, "clip": prev_clip}, "class_type": "CLIPTextEncode"},
        "3":  {"inputs": {"seed": 0, "steps": sampler["steps"], "cfg": sampler["cfg"],
                          "sampler_name": sampler["sampler_name"], "scheduler": sampler["scheduler"],
                          "denoise": 1.0, "model": prev_model, "positive": ["6", 0], "negative": ["7", 0],
                          "latent_image": ["5", 0]}, "class_type": "KSampler"},
        "8":  {"inputs": {"samples": ["3", 0], "vae": ["1", 0]}, "class_type": "VAEDecode"},
        "10": {"inputs": {"model_name": "4x-UltraSharp.pth"}, "class_type": "UpscaleModelLoader"},
        "11": {"inputs": {"upscale_model": ["10", 0], "image": ["8", 0]}, "class_type": "ImageUpscaleWithModel"},
        "9":  {"inputs": {"filename_prefix": "ComfyUI", "images": ["11", 0]}, "class_type": "SaveImage"},
    })
    return json.dumps(wf)


class Tools:
    class Valves(BaseModel):
        pass

    def __init__(self):
        self.valves = self.Valves()

    async def switch_model(self, model: str, __request__=None, __event_emitter__=None) -> str:
        """
        Switch the active ComfyUI image generation model with optimal LoRA stack.
        :param model: Model name. One of: juggernaut, pony, dreamshaper, realvis, anime, nova, comic
        """
        model = model.lower().strip()
        if model not in MODEL_MAP:
            return f"Unknown model '{model}'. Available: {', '.join(MODEL_MAP.keys())}"

        ckpt = MODEL_MAP[model]
        new_workflow = _build_workflow_with_loras(ckpt, model)
        sampler = SAMPLER_SETTINGS.get(model, {})
        loras = LORA_STACKS.get(model, [])

        conn = sqlite3.connect(DB_PATH)
        try:
            c = conn.cursor()
            wf_val = json.dumps(new_workflow)
            conn.execute("INSERT INTO config (key, value) VALUES ('image_generation.comfyui.workflow', ?) ON CONFLICT(key) DO UPDATE SET value=?", (wf_val, wf_val))
            
            model_val = json.dumps('"' + ckpt + '"')
            conn.execute("INSERT INTO config (key, value) VALUES ('image_generation.model', ?) ON CONFLICT(key) DO UPDATE SET value=?", (model_val, model_val))
            conn.commit()
        finally:
            conn.close()

        if __request__ and hasattr(__request__, "app"):
            try:
                __request__.app.state.config.COMFYUI_WORKFLOW = new_workflow
            except Exception:
                pass

        lora_info = ", ".join(l[0].replace(".safetensors", "") for l in loras) if loras else "none"
        return (
            f"Switched to **{model}** ({ckpt})\n"
            f"CFG: {sampler.get('cfg')} | Steps: {sampler.get('steps')} | Sampler: {sampler.get('sampler_name')}\n"
            f"LoRAs: {lora_info}"
        )

    async def list_models(self, __event_emitter__=None) -> str:
        """
        List all available ComfyUI image generation models with their LoRA stacks.
        """
        conn = sqlite3.connect(DB_PATH)
        try:
            c = conn.cursor()
            c.execute("SELECT value FROM config WHERE key='image_generation.comfyui.workflow'")
            row = c.fetchone()
            if row:
                wf_str = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                wf = json.loads(wf_str) if isinstance(wf_str, str) else wf_str
                active_ckpt = wf.get("4", {}).get("inputs", {}).get("ckpt_name", "")
            else:
                active_ckpt = ""
        finally:
            conn.close()

        lines = ["**Available ComfyUI Models:**\n"]
        categories = {
            "📸 Photorealistic": ["juggernaut", "realvis"],
            "🎨 Anime / Illustrious": ["anime", "nova"],
            "🖼️ Comic / Western": ["comic"],
            "🐴 Pony / Mixed": ["pony"],
            "✨ Stylized": ["dreamshaper"],
        }
        for cat, keys in categories.items():
            lines.append(f"\n{cat}")
            for key in keys:
                ckpt = MODEL_MAP[key]
                marker = "✅" if ckpt == active_ckpt else "  "
                loras = LORA_STACKS.get(key, [])
                s = SAMPLER_SETTINGS.get(key, {})
                lora_str = ", ".join(l[0].replace(".safetensors", "") for l in loras) if loras else "none"
                lines.append(f"{marker} **{key}** — CFG {s.get('cfg')}, {s.get('steps')} steps")
                lines.append(f"     LoRAs: {lora_str}")
        return "\n".join(lines)

    async def generate_video(self, prompt: str, model: str = "juggernaut", frames: int = 16, fps: int = 8, __request__=None, __event_emitter__=None) -> str:
        """
        Generate a short video clip using AnimateDiff. Sends workflow directly to ComfyUI API.
        :param prompt: Description of the video content
        :param model: Base model to use. One of: juggernaut, realvis, anime, nova
        :param frames: Number of frames (8=1s, 16=2s, 24=3s at 8fps). Default 16.
        :param fps: Frames per second for output video. Default 8.
        """
        import urllib.request
        import random

        model = model.lower().strip()
        ckpt = MODEL_MAP.get(model, MODEL_MAP["juggernaut"])
        seed = random.randint(0, 2**32 - 1)

        neg = NEGATIVE_PROMPTS.get(model, NEGATIVE_PROMPTS["default"])
        neg += ", static, flickering, bad motion, choppy, frozen"

        frames = max(8, min(32, frames))

        wf = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
            "2": {"class_type": "VAELoader", "inputs": {"vae_name": "sdxl_vae_fp16fix.safetensors"}},
            "3": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
            "4": {"class_type": "CLIPTextEncode", "inputs": {"text": neg, "clip": ["1", 1]}},
            "5": {"class_type": "ADE_AnimateDiffLoaderWithContext", "inputs": {
                "model": ["1", 0],
                "motion_model": "mm_sdxl_v10_beta.ckpt",
                "beta_schedule": "sqrt_linear (AnimateDiff)",
                "motion_scale": 1.0,
                "apply_v2_models_properly": False,
                "context_options": ["6", 0]
            }},
            "6": {"class_type": "ADE_StandardStaticContextOptions", "inputs": {
                "context_length": min(frames, 16),
                "context_stride": 1,
                "context_overlap": 4,
                "fuse_method": "pyramid",
                "use_on_equal_length": False,
                "start_percent": 0.0,
                "guarantee_steps": 1
            }},
            "7": {"class_type": "EmptyLatentImage", "inputs": {"width": 768, "height": 512, "batch_size": frames}},
            "8": {"class_type": "KSampler", "inputs": {
                "seed": seed, "steps": 20, "cfg": 7.0,
                "sampler_name": "dpmpp_2m", "scheduler": "karras", "denoise": 1.0,
                "model": ["5", 0], "positive": ["3", 0], "negative": ["4", 0],
                "latent_image": ["7", 0]
            }},
            "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["2", 0]}},
            "10": {"class_type": "VHS_VideoCombine", "inputs": {
                "images": ["9", 0],
                "frame_rate": fps,
                "loop_count": 0,
                "filename_prefix": "AnimateDiff",
                "format": "video/h264-mp4",
                "pingpong": False,
                "save_output": True
            }}
        }

        payload = json.dumps({"prompt": wf}).encode()
        try:
            req = urllib.request.Request(
                "http://gpuhost.internal.familie-frischkorn.de:8188/api/prompt",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                prompt_id = result.get("prompt_id", "?")
        except Exception as e:
            return f"ComfyUI API error: {e}"

        dur = frames / fps
        return (
            f"Video generation started!\n"
            f"Model: **{model}** | Frames: {frames} | FPS: {fps} | Duration: {dur:.1f}s\n"
            f"Prompt ID: `{prompt_id}`\n"
            f"Output: http://gpuhost.internal.familie-frischkorn.de:8188 → History → AnimateDiff_*.mp4"
        )

