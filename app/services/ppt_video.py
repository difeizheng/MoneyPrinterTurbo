"""
PPT 转视频「按页对齐」管线。

与通用的 task.start / combine_videos「扁管线」不同：这里按 PPT 每一页独立处理——
每页单独 TTS、每页画面持续时间 = 该页旁白音频的实际时长、每页字幕随该页画面烧入。
从而保证「画面 ↔ 旁白 ↔ 字幕」三者按页严格对齐，不再统一固定时长。

流程：
  ① 按页 TTS          → 每页 audio_page_i.mp3 + SubMaker + 真实时长
  ② 按页字幕          → 每页 sub_page_i.srt（短文本，edge 匹配几乎必成）
  ③ 合并字幕（累计偏移）→ subtitle.srt（时间轴与拼接音频天然对齐）
  ④ 拼接音频          → audio.mp3
  ⑤ 按页拼静音视频    → combined-1.mp4（每页时长 = 该页音频）
  ⑥ 复用 video.generate_video 收尾（烧字幕 + 挂音频 + 混 BGM + 渲染）

不动通用 task.py / video.py / voice.py / subtitle.py，全部复用现有函数。
"""

import os
import re
import subprocess
from typing import List, Tuple

from loguru import logger

from app.models.schema import VideoAspect, VideoParams
from app.services import subtitle, video, voice
from app.utils import utils

# SRT 时间戳形如 "00:01:23,500 --> 00:01:27,250"
_SRT_TS_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})"
)


# ===================== 辅助：SRT 时间戳 =====================


def _hmsm_to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _parse_srt_range(ts_range: str) -> Tuple[float, float]:
    """把 'HH:MM:SS,mmm --> HH:MM:SS,mmm' 解析成 (start_sec, end_sec)。"""
    match = _SRT_TS_RE.search(ts_range or "")
    if not match:
        return 0.0, 0.0
    g = match.groups()
    start = _hmsm_to_seconds(g[0], g[1], g[2], g[3])
    end = _hmsm_to_seconds(g[4], g[5], g[6], g[7])
    return start, end


# ===================== 辅助：按页字幕合并（累计偏移） =====================


def merge_page_srts(
    page_srt_paths: List[str], page_durations: List[float], out_path: str
) -> str:
    """
    把每页 SRT 拼成一条，第 i 页的时间戳整体偏移 sum(durations[:i])。

    与拼接音频的顺序一致，因此合并字幕的时间轴与最终音频/画面天然对齐。
    任一页 SRT 缺失（该页字幕生成失败）则跳过该页的字幕，但偏移仍按其音频时长累加，
    保证后续页字幕不错位。返回写入的 out_path（无任何有效块时返回 ""）。
    """
    blocks: List[str] = []
    idx = 0
    offset = 0.0
    for srt_path, dur in zip(page_srt_paths, page_durations):
        if srt_path and os.path.isfile(srt_path):
            for _orig_idx, ts_range, line in subtitle.file_to_subtitles(srt_path):
                if not line:
                    continue
                start, end = _parse_srt_range(ts_range)
                idx += 1
                # text_to_srt 接收秒，内部转 HH:MM:SS,mmm
                block = utils.text_to_srt(idx, line, start + offset, end + offset)
                blocks.append(block.strip())
        offset += dur

    if not blocks:
        return ""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(blocks) + "\n")
    return out_path


# ===================== 辅助：contain + 黑边 =====================


def _contain_target_size(clip_w: int, clip_h: int, width: int, height: int) -> Tuple[int, int]:
    """
    纯函数：计算 contain（短边填满）缩放后的目标尺寸。
    - 与目标同尺寸 → 原样
    - 同比例 → 正好 (width, height)
    - 否则按短边缩放，长边溢出由 letterbox 黑边承载
    """
    if clip_w == width and clip_h == height:
        return (width, height)
    clip_ratio = clip_w / clip_h
    video_ratio = width / height
    if clip_ratio == video_ratio:
        return (width, height)
    scale_factor = width / clip_w if clip_ratio > video_ratio else height / clip_h
    return (int(clip_w * scale_factor), int(clip_h * scale_factor))


def _contain_letterbox(clip, width: int, height: int, duration: float):
    """
    把任意尺寸的图片 clip 缩放到目标比例（contain：短边填满，长边居中黑边）。
    复刻 video.combine_videos:624-644 的内联逻辑，不改 video.py。
    """
    from moviepy import ColorClip, CompositeVideoClip

    clip_w, clip_h = clip.size
    new_width, new_height = _contain_target_size(clip_w, clip_h, width, height)
    if (new_width, new_height) == (clip_w, clip_h):
        return clip
    if (new_width, new_height) == (width, height):
        return clip.resized(new_size=(width, height))

    background = ColorClip(size=(width, height), color=(0, 0, 0)).with_duration(
        duration
    )
    foreground = clip.resized(new_size=(new_width, new_height)).with_position(
        "center"
    )
    return CompositeVideoClip([background, foreground], size=(width, height))


# ===================== 辅助：拼接按页音频 =====================


def _concat_page_audios(page_mp3_paths: List[str], out_path: str) -> str:
    """
    把每页 mp3 顺序拼成一条。优先 pydub（干净重编码），失败回退 ffmpeg concat。
    """
    if not page_mp3_paths:
        return ""

    # 优先 pydub
    try:
        from pydub import AudioSegment

        combined = AudioSegment.empty()
        for mp3 in page_mp3_paths:
            combined += AudioSegment.from_mp3(mp3)
        combined.export(out_path, format="mp3")
        if os.path.isfile(out_path):
            return out_path
    except Exception as e:
        logger.warning(f"pydub concat failed, fallback to ffmpeg: {e}")

    # 回退：ffmpeg concat demuxer
    try:
        list_file = out_path + ".concat.txt"
        with open(list_file, "w", encoding="utf-8") as f:
            for mp3 in page_mp3_paths:
                norm = mp3.replace("\\", "/").replace("'", "'\\''")
                f.write(f"file '{norm}'\n")
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", list_file, "-c:a", "libmp3lame", "-q:a", "2", out_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        if os.path.isfile(list_file):
            os.remove(list_file)
        return out_path if os.path.isfile(out_path) else ""
    except Exception as e:
        logger.error(f"ffmpeg audio concat also failed: {e}")
        return ""


# ===================== 主入口 =====================


def generate_ppt_video(
    task_id: str, params: VideoParams, pages: List[Tuple[str, str]]
) -> dict:
    """
    按页生成 PPT 视频。

    Args:
        task_id: 任务 id（决定产物目录）。
        params: VideoParams（用 video_aspect / voice_name / voice_rate / voice_volume /
                subtitle_enabled / font_* / bgm_* / n_threads 等；video_script 不再使用）。
        pages: [(image_path, narration_text), ...]，按页顺序。image_path 必须已落盘。

    Returns:
        {"videos": [final_path], "combined_videos": [combined_path]}，失败抛异常。
    """
    if not pages:
        raise ValueError("pages is empty")

    task_dir = utils.task_dir(task_id)
    voice_name = voice.parse_voice_name(params.voice_name)
    voice_rate = getattr(params, "voice_rate", 1.0) or 1.0
    voice_volume = getattr(params, "voice_volume", 1.0) or 1.0

    # ---------- ① 按页 TTS ----------
    logger.info(f"\n\n## PPT 按页 TTS：共 {len(pages)} 页")
    page_mp3s: List[str] = []
    page_durations: List[float] = []
    page_submakers = []
    page_texts: List[str] = []
    for i, (image_path, text) in enumerate(pages):
        text = (text or "").strip()
        if not text:
            raise ValueError(f"page {i} narration is empty (image: {image_path})")
        audio_i = os.path.join(task_dir, f"audio_page_{i}.mp3")
        logger.info(f"TTS page {i}/{len(pages) - 1}, voice: {voice_name}")
        sub_maker = voice.tts(
            text=text,
            voice_name=voice_name,
            voice_rate=voice_rate,
            voice_file=audio_i,
            voice_volume=voice_volume,
        )
        if sub_maker is None:
            raise RuntimeError(
                f"page {i} TTS failed (voice/language/network); "
                f"check edge_tts_timeout and proxy in config.toml"
            )
        dur = voice.get_audio_duration(sub_maker)
        if dur <= 0:
            raise RuntimeError(f"page {i} audio duration is 0")
        page_mp3s.append(audio_i)
        page_durations.append(dur)
        page_submakers.append(sub_maker)
        page_texts.append(text)

    logger.info(f"按页时长: {[round(d, 2) for d in page_durations]}")

    # ---------- ② 按页字幕 ----------
    page_srt_paths: List[str] = []
    if params.subtitle_enabled:
        logger.info("\n\n## PPT 按页字幕")
        for i, (sub_maker, text) in enumerate(zip(page_submakers, page_texts)):
            srt_i = os.path.join(task_dir, f"sub_page_{i}.srt")
            try:
                voice.create_subtitle(sub_maker, text, srt_i)
                if os.path.isfile(srt_i):
                    page_srt_paths.append(srt_i)
                else:
                    # 单页短文本理论上必成；个别失败则该页无字幕，不致命
                    page_srt_paths.append("")
                    logger.warning(f"page {i} subtitle not generated (skipped)")
            except Exception as e:
                page_srt_paths.append("")
                logger.warning(f"page {i} subtitle failed: {e}")

    # ---------- ③ 合并字幕（累计偏移） ----------
    subtitle_path = ""
    if params.subtitle_enabled and any(page_srt_paths):
        subtitle_path = os.path.join(task_dir, "subtitle.srt")
        merge_page_srts(page_srt_paths, page_durations, subtitle_path)
        if not os.path.isfile(subtitle_path):
            subtitle_path = ""
        else:
            logger.info(f"合并字幕: {subtitle_path}")

    # ---------- ④ 拼接音频 ----------
    logger.info("\n\n## 拼接按页音频")
    audio_path = os.path.join(task_dir, "audio.mp3")
    if len(page_mp3s) == 1:
        # 单页直接复用，避免一次无谓重编码
        import shutil

        shutil.copyfile(page_mp3s[0], audio_path)
    else:
        audio_path = _concat_page_audios(page_mp3s, audio_path)
        if not audio_path:
            raise RuntimeError("failed to concat page audios")

    # ---------- ⑤ 按页拼静音视频 ----------
    logger.info("\n\n## 按页拼装静音视频（每页时长 = 该页音频）")
    aspect = VideoAspect(params.video_aspect)
    width, height = aspect.to_resolution()
    combined_path = os.path.join(task_dir, "combined-1.mp4")

    page_clips = []
    try:
        for i, ((image_path, _text), dur) in enumerate(zip(pages, page_durations)):
            base_clip, _used_path = video._open_image_clip_with_fallback(image_path)
            base_clip = base_clip.with_duration(dur)
            clip = _contain_letterbox(base_clip, width, height, dur)
            page_clips.append(clip)

        from moviepy import concatenate_videoclips

        silent = concatenate_videoclips(page_clips, method="compose")
        # 静音视频（audio=False）：无需临时音频文件/音频参数，最小化写盘参数
        video._write_videofile_with_codec_fallback(
            silent,
            output_file=combined_path,
            codec=video._get_configured_video_codec(),
            fps=30,
            audio=False,
            threads=getattr(params, "n_threads", 2) or 2,
            logger=None,
        )
    finally:
        for c in page_clips:
            try:
                video.close_clip(c)
            except Exception:
                pass

    if not os.path.isfile(combined_path):
        raise RuntimeError("failed to assemble per-page silent video")

    # ---------- ⑥ 复用 generate_video 收尾（烧字幕 + 挂音频 + 混 BGM + 渲染） ----------
    final_path = os.path.join(task_dir, "final-1.mp4")
    logger.info(
        f"\n\n## generate_video 收尾：silent={combined_path}, "
        f"audio={audio_path}, subtitle={subtitle_path or '(无)'}, out={final_path}"
    )
    video.generate_video(
        video_path=combined_path,
        audio_path=audio_path,
        subtitle_path=subtitle_path,
        output_file=final_path,
        params=params,
    )

    if not os.path.isfile(final_path):
        raise RuntimeError("generate_video did not produce final output")

    return {"videos": [final_path], "combined_videos": [combined_path]}
