"""红线守卫：授权照片必须永远不进 Git。

参与者自愿提供的人脸照片，约定是「只在本机、不上传云端、不外泄」。
`dataset/real/README.md` 与 `dataset/real/faces/README.md` 写了这条规则，
但规则本身不拦人：改一行 `.gitignore`、或者 `git add -f`，照片就进历史了，
而 Git 历史洗不掉。

所以这里把规则变成断言，三层：

1. 索引里不许出现照片（除四个允许的说明/示例文件）；
2. 代表性照片路径必须仍被忽略（防止有人把例外规则放宽）；
3. 四个说明文件必须仍在跟踪（防止有人为了「干净」把文档也忽略掉）。

这是本仓库唯一一条「宁可不方便也要拦」的测试：失败时首先怀疑自己，
而不是去改断言。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

# 允许进库的仅限说明与示例，任何真实照片 / 本地清单都不在白名单里
ALLOWED_TRACKED = {
    "dataset/real/README.md",
    "dataset/real/manifest.example.csv",
    "dataset/real/faces/README.md",
    "dataset/real/faces/manifest.example.csv",
}

# 这些路径必须被忽略：覆盖常见图片格式与工具生成的本地清单
MUST_BE_IGNORED = (
    "dataset/real/faces/images/face-dev-001.jpg",
    "dataset/real/faces/images/face-dev-001.png",
    "dataset/real/faces/face-001.jpg",
    "dataset/real/faces/face-001.webp",
    "dataset/real/real-004.jpg",
    "dataset/real/faces/manifest.local.csv",
    "dataset/real/faces/summary.local.json",
    "dataset/real/faces/CONSENT.md",
    "work/upload-abc.jpg",
    "results/app/export.png",
    "dist/PhotoCheck_App.zip",
)


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )


@unittest.skipIf(shutil.which("git") is None, "环境无 git，跳过红线守卫")
class AuthorisedPhotosStayLocal(unittest.TestCase):
    def test_index_contains_no_photographs(self):
        out = git("ls-files", "dataset/real").stdout.split()
        unexpected = sorted(set(out) - ALLOWED_TRACKED)
        self.assertEqual(
            unexpected,
            [],
            "授权照片或其本地清单已被 Git 跟踪："
            f"{unexpected}。请立即 git rm --cached，并检查是否已推送到远端——"
            "一旦推送，必须视为已经外泄。",
        )

    def test_photo_paths_are_still_ignored(self):
        for path in MUST_BE_IGNORED:
            with self.subTest(path=path):
                self.assertEqual(
                    git("check-ignore", "-q", path).returncode,
                    0,
                    f"{path} 不再被忽略，忽略规则可能被放宽了",
                )

    def test_documentation_files_stay_tracked(self):
        out = set(git("ls-files", "dataset/real").stdout.split())
        missing = sorted(ALLOWED_TRACKED - out)
        self.assertEqual(
            missing, [], f"说明/示例文件不应被忽略，丢失后规则就没人看得见：{missing}"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
