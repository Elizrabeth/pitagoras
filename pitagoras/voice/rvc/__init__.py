"""RVC voice conversion: turns any TTS voice into *your* voice, using an RVC model
(the .pth + .index files you get from RVC / Applio training).

    voices\\<name>\\<anything>.pth     the model (required)
    voices\\<name>\\<anything>.index   the feature index (optional, makes it sound more like you)

The TTS speaks first (e.g. a Polish neural voice), then every sentence goes through:
ContentVec (what is being said) + RMVPE (pitch) -> your model's synthesizer -> your voice.
Needs PyTorch; runs on the GPU in a fraction of a second per sentence.

No fairseq: ContentVec is loaded through `transformers` (lengyue233/content-vec-best),
and RMVPE + ContentVec weights are downloaded from Hugging Face on first use.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from ...config import ROOT

log = logging.getLogger("rvc")

VOICES_DIR = ROOT / "voices"
CONTENTVEC_REPO = "lengyue233/content-vec-best"
RMVPE_REPO, RMVPE_FILE = "lj1995/VoiceConversionWebUI", "rmvpe.pt"

SR16 = 16000
WINDOW = 160  # samples per frame at 16 kHz (10 ms)
X_PAD = 1     # seconds of padding around each sentence


def list_models() -> list[str]:
    """Voice models in voices\\: a folder with a .pth (+ .index) inside, or a loose <name>.pth."""
    if not VOICES_DIR.is_dir():
        VOICES_DIR.mkdir(exist_ok=True)
        return []
    names = {d.name for d in VOICES_DIR.iterdir() if d.is_dir() and any(d.glob("*.pth"))}
    names |= {p.stem for p in VOICES_DIR.glob("*.pth")}
    return sorted(names, key=str.lower)


def model_files(name: str) -> tuple[Path, Path | None]:
    d = VOICES_DIR / name
    if d.is_dir():
        pths = sorted(d.glob("*.pth"), key=lambda p: p.stat().st_size, reverse=True)
        idx = sorted(d.glob("*.index"), key=lambda p: ("added" not in p.name, -p.stat().st_size))
    else:  # loose files: voices\<name>.pth and maybe an .index whose name contains <name>
        pths = [p for p in [VOICES_DIR / f"{name}.pth"] if p.exists()]
        idx = sorted((p for p in VOICES_DIR.glob("*.index") if name.lower() in p.stem.lower()),
                     key=lambda p: -p.stat().st_size)
    if not pths:
        raise FileNotFoundError(f"No .pth model for '{name}' in {VOICES_DIR}")
    return pths[0], (idx[0] if idx else None)


class RVC:
    def __init__(self, name: str, device: str = "cuda"):
        import torch
        from huggingface_hub import hf_hub_download
        from scipy import signal

        from .models import (SynthesizerTrnMs256NSFsid, SynthesizerTrnMs256NSFsid_nono,
                             SynthesizerTrnMs768NSFsid, SynthesizerTrnMs768NSFsid_nono)
        from .rmvpe import RMVPE

        self.torch = torch
        self.name = name
        self.device = torch.device("cuda:0" if device == "cuda" and torch.cuda.is_available() else "cpu")
        self.half = self.device.type == "cuda"
        pth, index_path = model_files(name)

        cpt = torch.load(pth, map_location="cpu", weights_only=False)
        self.tgt_sr = int(cpt["config"][-1])
        cpt["config"][-3] = cpt["weight"]["emb_g.weight"].shape[0]  # number of speakers
        self.if_f0 = int(cpt.get("f0", 1))
        self.version = cpt.get("version", "v1")
        cls = {("v1", 1): SynthesizerTrnMs256NSFsid, ("v1", 0): SynthesizerTrnMs256NSFsid_nono,
               ("v2", 1): SynthesizerTrnMs768NSFsid, ("v2", 0): SynthesizerTrnMs768NSFsid_nono}[(self.version, self.if_f0)]
        net = cls(*cpt["config"], is_half=self.half)
        del net.enc_q
        net.load_state_dict(cpt["weight"], strict=False)
        net.eval().to(self.device)
        self.net = net.half() if self.half else net.float()
        del cpt

        # what is being said: ContentVec (= RVC's "hubert_base.pt"), without fairseq
        from transformers import HubertModel

        hub = HubertModel.from_pretrained(CONTENTVEC_REPO)
        hub.eval().to(self.device)
        self.hubert = hub.half() if self.half else hub.float()
        self.final_proj = None
        if self.version == "v1":  # v1 models use the 256-dim projection of layer 9
            sd = torch.load(hf_hub_download(CONTENTVEC_REPO, "pytorch_model.bin"), map_location="cpu", weights_only=True)
            proj = torch.nn.Linear(768, 256)
            proj.load_state_dict({"weight": sd["final_proj.weight"], "bias": sd["final_proj.bias"]})
            self.final_proj = (proj.half() if self.half else proj).to(self.device)

        # pitch
        self.rmvpe = RMVPE(hf_hub_download(RMVPE_REPO, RMVPE_FILE), is_half=self.half, device=self.device)

        # the index pulls the features toward your training data (more "you")
        self.index = self.big_npy = None
        if index_path:
            try:
                import faiss

                self.index = faiss.read_index(str(index_path))
                self.big_npy = self.index.reconstruct_n(0, self.index.ntotal)
            except Exception as e:  # noqa: BLE001
                log.warning("could not read %s (%s); converting without it", index_path.name, e)
        self.bh, self.ah = signal.butter(N=5, Wn=48, btype="high", fs=SR16)
        # warm up: the first GPU run sets up CUDA kernels (~15 s); do it now, not mid-sentence
        t = np.arange(SR16) / SR16
        self.convert((0.3 * np.sin(2 * np.pi * 140 * t) * (1 + np.sin(2 * np.pi * 3 * t))).astype(np.float32), SR16)
        log.info("rvc: '%s' (%s, %d Hz%s) on %s", name, self.version, self.tgt_sr,
                 ", with index" if self.index is not None else "", self.device)

    # ── conversion ───────────────────────────────────────────
    def _f0(self, x: np.ndarray, p_len: int, key: float):
        f0 = self.rmvpe.infer_from_audio(x, thred=0.03)
        f0 = f0[:p_len] * pow(2, key / 12)
        if len(f0) < p_len:
            f0 = np.pad(f0, (0, p_len - len(f0)))
        f0_min, f0_max = 50, 1100
        mel_min, mel_max = 1127 * np.log(1 + f0_min / 700), 1127 * np.log(1 + f0_max / 700)
        mel = 1127 * np.log(1 + f0 / 700)
        mel[mel > 0] = (mel[mel > 0] - mel_min) * 254 / (mel_max - mel_min) + 1
        mel[mel <= 1] = 1
        mel[mel > 255] = 255
        return np.rint(mel).astype(np.int64), f0.astype(np.float32)

    def convert(self, audio: np.ndarray, sr: int, pitch: float = 0, index_rate: float = 0.5,
                protect: float = 0.33) -> tuple[np.ndarray, int]:
        import librosa
        from scipy import signal

        torch = self.torch
        F = torch.nn.functional
        x = audio.astype(np.float32)
        if sr != SR16:
            x = librosa.resample(x, orig_sr=sr, target_sr=SR16)
        peak = np.abs(x).max() / 0.95
        if peak > 1:
            x = x / peak
        x = signal.filtfilt(self.bh, self.ah, x).astype(np.float32)
        t_pad = SR16 * X_PAD
        t_pad_tgt = self.tgt_sr * X_PAD
        xp = np.pad(x, (t_pad, t_pad), mode="reflect")
        p_len = xp.shape[0] // WINDOW
        dtype = torch.float16 if self.half else torch.float32

        with torch.no_grad():
            feats_in = torch.from_numpy(xp).to(self.device, dtype).view(1, -1)
            if self.version == "v1":
                hs = self.hubert(feats_in, output_hidden_states=True).hidden_states[9]
                feats = self.final_proj(hs)
            else:
                feats = self.hubert(feats_in).last_hidden_state
            feats0 = feats.clone()

            if self.index is not None and index_rate > 0:
                npy = feats[0].float().cpu().numpy()
                score, ix = self.index.search(npy, k=8)
                weight = np.square(1 / np.maximum(score, 1e-9))
                weight /= weight.sum(axis=1, keepdims=True)
                npy = np.sum(self.big_npy[ix] * np.expand_dims(weight, axis=2), axis=1)
                feats = torch.from_numpy(npy).unsqueeze(0).to(self.device, dtype) * index_rate + (1 - index_rate) * feats

            feats = F.interpolate(feats.permute(0, 2, 1), scale_factor=2).permute(0, 2, 1)
            feats0 = F.interpolate(feats0.permute(0, 2, 1), scale_factor=2).permute(0, 2, 1)
            p_len = min(p_len, feats.shape[1])
            feats, feats0 = feats[:, :p_len], feats0[:, :p_len]
            sid = torch.tensor([0], device=self.device).long()
            plen = torch.tensor([p_len], device=self.device).long()
            if self.if_f0:
                coarse, f0 = self._f0(xp, p_len, pitch)
                pitch_t = torch.from_numpy(coarse).to(self.device).unsqueeze(0)
                pitchf = torch.from_numpy(f0).to(self.device).unsqueeze(0)
                if protect < 0.5:  # keep breaths/consonants (unvoiced parts) closer to the original
                    m = pitchf.clone()
                    m[pitchf > 0] = 1
                    m[pitchf < 1] = protect
                    m = m.unsqueeze(-1).to(dtype)
                    feats = feats * m + feats0 * (1 - m)
                out = self.net.infer(feats, plen, pitch_t, pitchf, sid)[0][0, 0]
            else:
                out = self.net.infer(feats, plen, sid)[0][0, 0]
            y = out.float().cpu().numpy()
        y = y[t_pad_tgt: len(y) - t_pad_tgt] if len(y) > 2 * t_pad_tgt else y
        peak = np.abs(y).max() / 0.99 if len(y) else 0
        if peak > 1:
            y = y / peak
        return y.astype(np.float32), self.tgt_sr

    def close(self) -> None:
        del self.net, self.hubert, self.rmvpe
        if self.torch.cuda.is_available():
            self.torch.cuda.empty_cache()
