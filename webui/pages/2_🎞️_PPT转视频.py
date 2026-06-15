"""
PPT 转视频页（Streamlit 传统 multipage）。

放在 webui/pages/ 下，Streamlit 会在侧边栏自动生成「🎞️ PPT 转视频」入口。

流程：
  1. 上传用 PowerPoint 导出的每页 PNG/JPG 图片（按文件名页码排序）
  2. 用视觉模型（默认 Qwen-VL）逐页识别并生成讲解旁白，可在页面上编辑
  3. 复用 app.services.task.start 的进程内生成管线出片

视觉模型 key 只存会话，不写 config.toml，避免往磁盘写密钥。
"""

import base64
import os
import re
import sys
import time
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

# ===================== 上传幻灯片 =====================

uploaded_files = st.file_uploader(
    tr("Upload Slide Images"),
    type=["png", "jpg", "jpeg"],
    accept_multiple_files=True,
    key="ppt_slide_uploader",
)
st.caption(tr("Slide Upload Help"))

sorted_files: list = sorted(uploaded_files, key=lambda f: f.name) if uploaded_files else []
sorted_names = [f.name for f in sorted_files]

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
        total = len(sorted_files)
        progress = st.progress(0.0, text=tr("Generating Narration"))
        status = st.empty()
        client = OpenAI(api_key=vl_key, base_url=vl_base)
        failed = 0
        for i, f in enumerate(sorted_files, 1):
            status.info(f"{tr('Generating Narration')} ({i}/{total}): {f.name}")
            text = _narrate_one(client, vl_model, f)
            if not text:
                failed += 1
            st.session_state[_safe_key(f.name)] = text
            progress.progress(i / total, text=f"{i}/{total}")
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
