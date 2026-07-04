"""
PPT 按页管线 ppt_video 的纯逻辑单测。

集成层面（按页 TTS / MoviePy 拼装 / LibreOffice）依赖外部服务，在 Docker webui
内手动端到端验证，不入此自动测试。这里只覆盖可纯函数化的部分：
  - merge_page_srts 的累计偏移合并
  - _parse_srt_range / _hmsm_to_seconds 时间戳解析
  - _contain_target_size contain 缩放尺寸决策

遵循项目测试约定：unittest，放 test/services/（否则被 .gitignore 拦掉）。
"""

import os
import tempfile
import unittest

from app.services import ppt_video


def _write_srt(path: str, blocks: list[tuple[int, str, str, str]]) -> None:
    """写一个最小 SRT。blocks = [(idx, start_ts, end_ts, text)]，ts 形如 00:00:01,500。"""
    with open(path, "w", encoding="utf-8") as f:
        for idx, start, end, text in blocks:
            f.write(f"{idx}\n{start} --> {end}\n{text}\n\n")


class TestParseSrtRange(unittest.TestCase):
    def test_basic_range(self):
        start, end = ppt_video._parse_srt_range("00:00:01,500 --> 00:00:04,250")
        self.assertAlmostEqual(start, 1.5)
        self.assertAlmostEqual(end, 4.25)

    def test_minutes_hours(self):
        start, end = ppt_video._parse_srt_range("01:02:03,000 --> 01:02:05,500")
        self.assertAlmostEqual(start, 1 * 3600 + 2 * 60 + 3)
        self.assertAlmostEqual(end, 1 * 3600 + 2 * 60 + 5.5)

    def test_garbage_returns_zero(self):
        self.assertEqual(ppt_video._parse_srt_range("not a timestamp"), (0.0, 0.0))
        self.assertEqual(ppt_video._parse_srt_range(""), (0.0, 0.0))


class TestMergePageSrts(unittest.TestCase):
    def test_cumulative_offset_and_index_continuity(self):
        with tempfile.TemporaryDirectory() as tmp:
            srt0 = os.path.join(tmp, "p0.srt")
            srt1 = os.path.join(tmp, "p1.srt")
            out = os.path.join(tmp, "subtitle.srt")
            # 第 0 页：0~3s 内两条字幕
            _write_srt(
                srt0,
                [(1, "00:00:00,500", "00:00:01,500", "甲"), (2, "00:00:01,500", "00:00:03,000", "乙")],
            )
            # 第 1 页：0~2s 内一条字幕
            _write_srt(srt1, [(1, "00:00:00,000", "00:00:02,000", "丙")])

            # 第 0 页音频 3s → 第 1 页整体偏移 3s
            result = ppt_video.merge_page_srts([srt0, srt1], [3.0, 2.0], out)
            self.assertEqual(result, out)
            self.assertTrue(os.path.isfile(out))

            from app.services import subtitle

            parsed = subtitle.file_to_subtitles(out)
            # 3 条，序号 1..3 连续
            self.assertEqual([p[0] for p in parsed], [1, 2, 3])
            # 第 1 页的字幕“丙”应落在 3s ~ 5s
            last_idx, last_range, last_text = parsed[2]
            start, end = ppt_video._parse_srt_range(last_range)
            self.assertAlmostEqual(start, 3.0)
            self.assertAlmostEqual(end, 5.0)
            self.assertEqual(last_text, "丙")

    def test_missing_page_srt_keeps_offset(self):
        """某页字幕缺失：跳过该页字幕，但偏移仍累加，后续页不错位。"""
        with tempfile.TemporaryDirectory() as tmp:
            srt1 = os.path.join(tmp, "p1.srt")
            out = os.path.join(tmp, "subtitle.srt")
            _write_srt(srt1, [(1, "00:00:00,000", "00:00:01,000", "后")])
            # 第 0 页 srt 路径为空，但音频 5s 仍计入偏移
            result = ppt_video.merge_page_srts(["", srt1], [5.0, 1.0], out)
            self.assertEqual(result, out)
            from app.services import subtitle

            parsed = subtitle.file_to_subtitles(out)
            self.assertEqual(len(parsed), 1)
            start, end = ppt_video._parse_srt_range(parsed[0][1])
            self.assertAlmostEqual(start, 5.0)
            self.assertAlmostEqual(end, 6.0)

    def test_all_empty_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "subtitle.srt")
            result = ppt_video.merge_page_srts(["", ""], [1.0, 2.0], out)
            self.assertEqual(result, "")
            self.assertFalse(os.path.isfile(out))


class TestContainTargetSize(unittest.TestCase):
    def test_same_size_passthrough(self):
        self.assertEqual(ppt_video._contain_target_size(1920, 1080, 1920, 1080), (1920, 1080))

    def test_same_ratio_exact_fit(self):
        # 16:9 → 16:9，直接目标尺寸
        self.assertEqual(ppt_video._contain_target_size(1280, 720, 1920, 1080), (1920, 1080))

    def test_wider_clip_letterboxed(self):
        # clip 比 target 更宽（2:1 进 16:9）：contain 按宽限制，高不足补上下黑边
        new_w, new_h = ppt_video._contain_target_size(2000, 1000, 1920, 1080)
        self.assertEqual(new_w, 1920)  # 宽填满
        self.assertEqual(new_h, 960)   # 1000 * (1920/2000)，上下留黑边

    def test_taller_clip_letterboxed(self):
        # clip 比 target 更高（9:16 进 16:9）：contain 按高限制，宽不足补左右黑边
        new_w, new_h = ppt_video._contain_target_size(900, 1600, 1920, 1080)
        self.assertEqual(new_h, 1080)  # 高填满
        self.assertEqual(new_w, 607)   # 900 * (1080/1600)，左右留黑边


if __name__ == "__main__":
    unittest.main()
