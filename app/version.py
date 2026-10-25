"""版本与项目元信息 —— 单一来源。

改版本号只改这个文件；前端「关于」弹窗和后端 /api/bootstrap 都读它。
发布时 git tag（vX.Y.Z）与这里的 VERSION 必须一致：
CI 用 tag 给 Docker Hub 镜像打版本标签（见 .github/workflows/image.yml），
两者不一致会出现「镜像叫 v1.0.0、关于里写着 1.0.1」这种对不上的情况。

版本递进规则（2026-10-18 起，用户定的）：
  每次发版**第三位 +1**（0.0.1 步进）；第三位**满 10 进位**到第二位，
  即 1.1.9 → 1.2.0，**不会出现 1.1.10**。第一位只在重大改版时动。
"""

VERSION = "1.4.20"
PROJECT_URL = "https://github.com/yuwancumian2009/NavPage"
CHANGELOG_URL = PROJECT_URL + "/blob/main/CHANGELOG.md"


def meta() -> dict:
    """给 /api/bootstrap 的公开元信息（不含任何敏感字段）。"""
    return {
        "version": VERSION,
        "project_url": PROJECT_URL,
        "changelog_url": CHANGELOG_URL,
    }
