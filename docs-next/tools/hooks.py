"""Page presentation shared by local and Read the Docs builds."""

from urllib.parse import urljoin


def on_page_markdown(markdown, page, config, files):
    relative = page.file.src_uri.removeprefix("zh/")
    if relative not in config.extra.get("ucm_fallback_pages", []):
        return markdown
    page.meta["ucm_fallback_language"] = "en"
    if page.edit_url:
        page.edit_url = page.edit_url.replace(
            "/docs-next/docs/zh/", "/docs-next/docs/en/", 1
        )
    english_url = urljoin(config.extra["ucm_english_url"], page.url)
    return (
        '!!! note "英文原文"\n\n'
        "    本页尚未提供中文翻译，以下展示英文原文。"
        f"[查看同版本英文页面]({english_url})。\n\n" + markdown
    )
