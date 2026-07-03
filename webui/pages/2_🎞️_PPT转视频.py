"""
PPT 转视频页（Streamlit 传统 multipage）。

放在 webui/pages/ 下，Streamlit 会在侧边栏自动生成「🎞️ PPT 转视频」入口。

流程：
  1. 上传 PNG/JPG 图片，或直接上传 .pptx
  2. pptx 会自动转图 + 读备注：有备注的页直接用备注作旁白，没有备注的页用视觉模型识别
  3. 用户在页面上编辑每页文案
  4. 复用 app.services.task.start 的进程内生成管线出片

视觉模型 key 只存会话，不写 config.toml，避免往磁盘写密钥。
"""

import base64
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import streamlit as st
from loguru import logger
from openai import OpenAI

# 项目根目录：webui/pages/<本文件>，需要三层 dirname
root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
if root_dir not in sys.path:
    sys.path.append(root_dir)

from app.config import config  # noqa: E402
from app.models.schema import (  # noqa: E402
    MaterialInfo,
    VideoAspect,
    VideoConcatMode,
    VideoParams,
)
from app.services import task as tm  # noqa: E402
from app.utils import utils  # noqa: E402
from app.utils import pptx_converter  # noqa: E402

st.set_page_config(
    page_title="PPT 转视频 - MoneyPrinterTurbo",
    page_icon="🎞️",
    layout="wide",
)

i18n_dir = os.path.join(root_dir, "webui", "i18n")
locales = utils.load_locales(i18n_dir)

if "ui_language" not in st.session_state:
    st.session_state["ui_language"] = config.ui.get(
        "language", utils.get_system_locale()
    )


def tr(key: str) -> str:
    """自包含 i18n 查询（与 Main.py / 历史任务页同逻辑）。"""
    loc = locales.get(st.session_state.get("ui_language", ""), {})
    return loc.get("Translation", {}).get(key, key)


# 旁白生成提示词（与 ppt_to_video.py 保持一致，正式投标讲解口吻）
NARRATION_PROMPT = """你是一个专业的投标方案讲解员。请根据这张PPT页面的内容，写一段用于视频旁白的讲解文案。

要求：
1. 语言正式、专业，适合投标汇报场景
2. 控制在 50-100 字左右（约 15-30 秒语音时长）
3. 直接说内容，不要说"这一页展示了"、"接下来我们看到"之类的过渡语
4. 如果是架构图或流程图，描述其核心结构和流程
5. 如果是数据表格，提炼关键数据亮点
6. 如果是文字列表，用流畅的语句串联要点
7. 只返回旁白文案本身，不要加任何格式标记"""

# 视觉模型默认配置（仅会话内，不回写磁盘）
DEFAULT_VL_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_VL_MODEL = "qwen-vl-max"

# 配音选项（edge_tts，API 格式带性别后缀，voice.parse_voice_name 会剥离）
VOICE_OPTIONS = {
    "云希（男声）": "zh-CN-YunxiNeural-Male",
    "晓晓（女声）": "zh-CN-XiaoxiaoNeural-Female",
    "云健（男声）": "zh-CN-YunjianNeural-Male",
    "晓伊（女声）": "zh-CN-XiaoyiNeural-Female",
}


def _safe_key(name: str) -> str:
    """把文件名转成合法的 Streamlit widget key（仅 a-zA-Z0-9_）。"""
    return "narr_" + re.sub(r"[^a-zA-Z0-9_]", "_", name)


def _prune_stale_narrations(current_names: list[str]) -> None:
    """清掉不属于当前上传集合的旧解说 key，避免文件增删后错位。"""
    current_keys = {_safe_key(n) for n in current_names}
    stale = [
        k
        for k in list(st.session_state.keys())
        if k.startswith("narr_") and k not in current_keys
    ]
    for k in stale:
        del st.session_state[k]


def _narration_mime(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    return "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"


def _narrate_one(client: OpenAI, model: str, file) -> str:
    """用视觉模型为单页图片生成旁白，失败返回空串。"""
    try:
        b64 = base64.b64encode(file.getvalue()).decode("utf-8")
        mime = _narration_mime(file.name)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        },
                        {"type": "text", "text": NARRATION_PROMPT},
                    ],
                }
            ],
            max_tokens=300,
            temperature=0.7,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.error(f"narration failed for {file.name}: {e}")
        return ""


def _narrate_one_from_path(
    client: OpenAI, model: str, image_path: str, mime: str = "image/png"
) -> str:
    """视觉模型识别：图片从磁盘路径读，base64 编码。"""
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        },
                        {"type": "text", "text": NARRATION_PROMPT},
                    ],
                }
            ],
            max_tokens=300,
            temperature=0.7,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.error(f"vision narration failed for {image_path}: {e}")
        return ""


# ===================== 顶部：标题 + 语言选择 =====================

title_col, lang_col = st.columns([3, 1])

with title_col:
    st.title(f"🎞️ {tr('PPT to Video')}")

with lang_col:
    display_languages: list[str] = []
    selected_index = 0
    for i, code in enumerate(locales.keys()):
        display_languages.append(f"{code} - {locales[code].get('Language')}")
        if code == st.session_state.get("ui_language", ""):
            selected_index = i
    selected_language = st.selectbox(
        "Language / 语言",
        options=display_languages,
        index=selected_index,
        key="ppt_language_selector",
        label_visibility="collapsed",
    )
    if selected_language:
        code = selected_language.split(" - ")[0].strip()
        st.session_state["ui_language"] = code

st.caption(tr("PPT to Video Help"))

# ===================== 视觉模型配置（仅会话） =====================

# seed 一次默认值，之后用 widget key 读写
if "ppt_vl_key" not in st.session_state:
    st.session_state["ppt_vl_key"] = (
        config.app.get("qwen_vl_api_key") or config.app.get("openai_api_key") or ""
    )
if "ppt_vl_base" not in st.session_state:
    st.session_state["ppt_vl_base"] = DEFAULT_VL_BASE_URL
if "ppt_vl_model" not in st.session_state:
    st.session_state["ppt_vl_model"] = DEFAULT_VL_MODEL

with st.expander(tr("Vision Model Settings"), expanded=False):
    st.text_input(
        tr("Vision API Key"),
        key="ppt_vl_key",
        type="password",
        help=tr("Vision API Key Help"),
    )
    st.text_input(tr("Vision Base URL"), key="ppt_vl_base")
    st.text_input(tr("Vision Model Name"), key="ppt_vl_model")

vl_key = (st.session_state.get("ppt_vl_key") or "").strip()
vl_base = (st.session_state.get("ppt_vl_base") or "").strip() or DEFAULT_VL_BASE_URL
vl_model = (st.session_state.get("ppt_vl_model") or "").strip() or DEFAULT_VL_MODEL

# ===================== 上传幻灯片（图片 或 .pptx） =====================

uploaded_files = st.file_uploader(
    tr("Upload Slide Images"),
    type=["png", "jpg", "jpeg", "pptx"],
    accept_multiple_files=True,
    key="ppt_slide_uploader",
)
st.caption(tr("Slide Upload Help"))

# 分流：pptx 与图片互斥；一次只处理一份 PPT（多份取第一个）
pptx_files = [f for f in (uploaded_files or []) if f.name.lower().endswith(".pptx")]
image_files = [f for f in (uploaded_files or []) if not f.name.lower().endswith(".pptx")]

# 已处理的 PPT 指纹（避免 Streamlit rerun 时重复转）
if "ppt_processed_signature" not in st.session_state:
    st.session_state["ppt_processed_signature"] = None

if pptx_files and image_files:
    st.warning(tr("PPTX And Images Mixed"))

# === PPTX 流程 ===
slide_state: list[dict] = []  # 每个 dict: {name, image_path, source, narration, notes_raw}

if pptx_files:
    pptx_file = pptx_files[0]
    # 用 (name, size) 作为指纹，rerun 同一文件不再转
    sig = (pptx_file.name, pptx_file.size)
    if st.session_state["ppt_processed_signature"] != sig:
        st.session_state["ppt_processed_signature"] = sig
        st.session_state["ppt_last_pptx_name"] = pptx_file.name
        st.session_state["ppt_last_sources"] = None  # 触发下方转换
    if st.session_state.get("ppt_last_pptx_name") == pptx_file.name and st.session_state.get(
        "ppt_last_sources"
    ) is None:
        # 准备临时工作目录（session 内复用，streamlit rerun 不丢）
        pptx_workspace = utils.storage_dir("pptx_workspace", create=True)
        deck_dir = Path(pptx_workspace) / uuid4().hex[:12]
        deck_dir.mkdir(parents=True, exist_ok=True)
        pptx_path = deck_dir / pptx_file.name
        pptx_path.write_bytes(pptx_file.getbuffer())
        try:
            with st.spinner(tr("Processing PPTX")):
                notes = pptx_converter.extract_slide_notes(pptx_path)
                images = pptx_converter.pptx_to_images(pptx_path, deck_dir)
        except pptx_converter.PPTXConversionError as e:
            st.error(f"{tr('PPTX Conversion Failed')}: {e}")
            logger.error(f"PPTX conversion failed for {pptx_file.name}: {e}")
            st.session_state["ppt_last_sources"] = []  # 阻止重试
            st.stop()
        # 把每页 PNG 落盘到 local_videos，构造虚拟 UploadedFile 等价物（用本地路径）
        local_videos_dir = utils.storage_dir("local_videos", create=True)
        use_notes = pptx_converter.decide_narration_source(notes)
        with_notes_count = sum(1 for x in use_notes if x)
        without_notes_count = len(use_notes) - with_notes_count
        sources: list[dict] = []
        for i, img_path in enumerate(images, 1):
            target_name = f"slide_{i:03d}.png"
            target_path = Path(local_videos_dir) / f"pptx_{uuid4().hex[:8]}_{target_name}"
            shutil.copyfile(img_path, target_path)
            raw_notes = notes[i - 1] if i - 1 < len(notes) else None
            sources.append(
                {
                    "name": target_name,
                    "image_path": str(target_path),
                    "source": "notes" if use_notes[i - 1] else "pending",
                    "narration": raw_notes or "",
                    "notes_raw": raw_notes,
                }
            )
        st.session_state["ppt_last_sources"] = sources
        st.session_state["ppt_last_summary"] = {
            "total": len(sources),
            "with_notes": with_notes_count,
            "without_notes": without_notes_count,
            "pptx_name": pptx_file.name,
        }
        # 预填有备注的页
        for s in sources:
            if s["source"] == "notes" and s["narration"]:
                st.session_state[_safe_key(s["name"])] = s["narration"]
        st.rerun()

    sources = st.session_state.get("ppt_last_sources") or []
    summary = st.session_state.get("ppt_last_summary")
    if summary:
        st.info(
            tr("Notes Detected Summary").format(
                total=summary["total"],
                with_notes=summary["with_notes"],
                without_notes=summary["without_notes"],
            )
        )
    slide_state = sources
    sorted_files_meta = [(s["name"], s["image_path"]) for s in sources]
    # 把 path 拼成简单对象供下游统一处理
    class _LocalImg:
        def __init__(self, name: str, path: str):
            self.name = name
            self._path = path

        def getbuffer(self):
            with open(self._path, "rb") as f:
                return f.read()

    sorted_files = [_LocalImg(n, p) for n, p in sorted_files_meta]
    sorted_names = [n for n, _ in sorted_files_meta]
else:
    # 纯图片流程（原行为）
    sorted_files = sorted(image_files, key=lambda f: f.name) if image_files else []
    sorted_names = [f.name for f in sorted_files]
    # 清空旧的 PPTX 状态，避免来源混淆
    if st.session_state.get("ppt_last_sources"):
        st.session_state["ppt_last_sources"] = None
        st.session_state["ppt_last_summary"] = None
        st.session_state["ppt_processed_signature"] = None

if sorted_files:
    _prune_stale_narrations(sorted_names)

# ===================== 主题 + 生成解说 =====================

subject_col, btn_col = st.columns([4, 1])
with subject_col:
    if "ppt_subject" not in st.session_state:
        st.session_state["ppt_subject"] = "PPT 方案汇报"
    st.text_input(tr("Video Subject Label"), key="ppt_subject")

with btn_col:
    st.write("")  # 对齐高度
    st.write("")
    can_generate_narration = bool(sorted_files) and bool(vl_key)
    generate_narrations = st.button(
        tr("Generate Narrations"),
        type="primary",
        disabled=not can_generate_narration,
        use_container_width=True,
    )

if generate_narrations:
    if not vl_key:
        st.error(tr("Please Enter the Vision API Key"))
    else:
        # 只对「无备注的页 + 当前文案空」调 VL；已有备注的页保留
        pending_targets: list[tuple[int, object]] = []
        for i, f in enumerate(sorted_files):
            cur = (st.session_state.get(_safe_key(f.name), "") or "").strip()
            # slide_state 在 PPTX 模式下记录了 source==pending；纯图片模式视为都待识别
            is_pending = True
            if slide_state:
                is_pending = slide_state[i]["source"] == "pending"
            if is_pending and not cur:
                pending_targets.append((i, f))

        if not pending_targets:
            st.info(tr("Narrations Generated"))
        else:
            total = len(pending_targets)
            progress = st.progress(0.0, text=tr("Generating Narration"))
            status = st.empty()
            client = OpenAI(api_key=vl_key, base_url=vl_base)
            failed = 0
            for n, (i, f) in enumerate(pending_targets, 1):
                status.info(f"{tr('Generating Narration')} ({n}/{total}): {f.name}")
                if hasattr(f, "_path") and not hasattr(f, "file_id"):
                    # 来自 PPTX 转换的本地图片
                    text = _narrate_one_from_path(client, vl_model, f._path)
                else:
                    text = _narrate_one(client, vl_model, f)
                if not text:
                    failed += 1
                st.session_state[_safe_key(f.name)] = text
                progress.progress(n / total, text=f"{n}/{total}")
                time.sleep(1)  # 避免 API 限流
            progress.empty()
            msg = tr("Narrations Generated")
            if failed:
                msg += f"（{failed} 页失败，已留空，可在下方手动补充）"
            status.success(msg)

# ===================== 可编辑解说 =====================

existing = [
    (name, _safe_key(name))
    for name in sorted_names
    if _safe_key(name) in st.session_state
]

if not sorted_files:
    st.info(tr("No Slides Uploaded"))
elif not existing:
    st.info(tr("Generate Narration First"))
else:
    st.subheader(tr("Edit Narrations"))
    for i, (name, key) in enumerate(existing, 1):
        st.text_area(
            f"{tr('Slide')} {i}: {name}",
            key=key,
            height=120,
        )

# ===================== 视频参数 =====================

# 当前可用解说（按上传顺序，跳过空串）
narration_texts = [
    (st.session_state.get(_safe_key(name), "") or "").strip()
    for name in sorted_names
]
nonempty_texts = [t for t in narration_texts if t]

st.divider()
st.subheader(tr("Video Settings"))

param_col1, param_col2, param_col3 = st.columns(3)
with param_col1:
    voice_label = st.selectbox(
        tr("Voice"),
        options=list(VOICE_OPTIONS.keys()),
        index=0,
        key="ppt_voice",
    )
    voice_name = VOICE_OPTIONS[voice_label]
    clip_options = [5, 8, 10, 15, 20]
    clip_duration = st.selectbox(
        tr("Clip Duration Label"),
        options=clip_options,
        index=clip_options.index(10),
        key="ppt_clip_duration",
    )
with param_col2:
    aspect_map = {
        tr("Landscape"): VideoAspect.landscape.value,
        tr("Portrait"): VideoAspect.portrait.value,
    }
    aspect_label = st.selectbox(
        tr("Aspect"),
        options=list(aspect_map.keys()),
        index=0,
        key="ppt_aspect",
    )
    video_aspect = aspect_map[aspect_label]
    subtitle_enabled = st.checkbox(
        tr("Subtitle Label"), value=True, key="ppt_subtitle"
    )
with param_col3:
    bgm_volume = st.slider(
        tr("BGM Volume Label"),
        min_value=0.0,
        max_value=0.5,
        value=0.15,
        step=0.05,
        key="ppt_bgm_volume",
    )

# ===================== 生成视频 =====================

can_generate_video = bool(sorted_files) and bool(nonempty_texts)
gen_disabled = not can_generate_video

st.write("")
if st.button(
    tr("Generate Video"),
    type="primary",
    disabled=gen_disabled,
    use_container_width=False,
):
    if not sorted_files:
        st.error(tr("Need Images First"))
    elif not nonempty_texts:
        st.error(tr("Need Narration First"))
    else:
        full_script = "。".join(t.rstrip("。") for t in nonempty_texts)
        subject = (st.session_state.get("ppt_subject") or "").strip() or "PPT"

        # 落盘图片为本地素材（命名同 Main.py：file_id_name）
        local_videos_dir = utils.storage_dir("local_videos", create=True)
        materials: list[MaterialInfo] = []
        for f in sorted_files:
            if hasattr(f, "_path") and not hasattr(f, "file_id"):
                # 来自 PPTX 流程：已落盘，直接用
                file_path = f._path
            else:
                file_path = os.path.join(local_videos_dir, f"{f.file_id}_{f.name}")
                with open(file_path, "wb") as out:
                    out.write(f.getbuffer())
            m = MaterialInfo()
            m.provider = "local"
            m.url = file_path
            materials.append(m)

        task_id = str(uuid4())
        params = VideoParams(
            video_subject=subject,
            video_script=full_script,
            video_aspect=video_aspect,
            video_source="local",
            video_materials=materials,
            video_concat_mode=VideoConcatMode.sequential.value,
            video_clip_duration=clip_duration,
            voice_name=voice_name,
            subtitle_enabled=subtitle_enabled,
            font_name="STHeitiMedium.ttc",
            font_size=48,
            stroke_width=1.5,
            bgm_type="random",
            bgm_volume=bgm_volume,
            video_count=1,
        )

        logger.info(f"PPT to Video task: {task_id}")
        logger.info(utils.to_json(params))

        with st.spinner(tr("Generating Video")):
            result = tm.start(task_id=task_id, params=params)

        if not result or "videos" not in result:
            st.error(tr("Video Generation Failed"))
            logger.error(tr("Video Generation Failed"))
            st.stop()

        st.success(tr("Video Generation Completed"))
        for url in result.get("videos", []):
            st.video(url)
        # 刷新历史任务页缓存，让新任务立即可见
        st.cache_data.clear()
