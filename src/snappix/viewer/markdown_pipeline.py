"""post.md → 表示 HTML のパイプライン（Qt ウィジェット非依存の部品）.

:class:`~snappix.viewer.markdown_view.MarkdownView` とそのレンダータスクが
共有する、状態を持たない変換・解決の関数群:

* 画像参照の正規化と解決 — :func:`_strip_dot_slash` / :func:`_img_src_key` /
  :func:`resolve_markdown_image_path`
* レンダ後 HTML の整形 — :func:`_rewrap_img_paragraphs`（単一画像の段落を
  詰める）と表示ボックスの導出 :func:`_layout_box`
* 面の骨格 — :data:`_HTML_TEMPLATE` と、テーマトークンから色を差し込む
  :func:`_theme_style_args`、投稿メタカードを組む :func:`_post_header_html`
* 本文フォントの既定・下限・上限（:data:`DEFAULT_FONT_PT` /
  :data:`MIN_FONT_PT` / :data:`MAX_FONT_PT`）と同期レンダの本文長上限

``QTextBrowser`` に触らない純関数なので、GUI スレッドの高速経路からも
ワーカー（``markdown_view._render_post_body``）からも同じ実体を呼べる。依存の
向きは **パイプライン → ビューは無し**（``markdown_view`` を import しない）。
"""

from __future__ import annotations

import html
import re
import urllib.parse
from pathlib import Path

from PySide6.QtCore import QSize

from ..common.i18n import t
from ..common.post_meta import KEY_POST_ID as _KEY_POST_ID
from ..common.post_meta import KEY_POSTED_AT as _KEY_POSTED_AT
from ..common.post_meta import KEY_SERVICE as _KEY_SERVICE
from ..common.ui import current_tokens, rgba


def _strip_dot_slash(ref: str) -> str:
    """Drop a single leading ``./`` from a relative image reference.

    Not ``str.lstrip("./")`` — that strips *characters*, so a dotfile ref
    like ``./.cover.jpg`` lost its leading dot (→ ``cover.jpg``) and the
    ``_img_targets`` key no longer matched ``loadResource``'s
    ``relative_to``-derived key, forcing the slow sync-decode fallback.
    """
    return ref.removeprefix("./")


def _img_src_key(src: str) -> str:
    """``<img src="...">`` の属性値を ``_img_targets`` のキーへ正規化する。

    属性値は **2 重に符号化**されている: post.md 側の URL エンコード
    (``./%E7%B5%B5.jpg``) の上に、markdown-it が属性を書き出すときの HTML
    エスケープが乗る。``&`` を含むファイル名 (``a&b.jpg``) は素の post.md
    では ``src="./a&amp;b.jpg"`` になり、``unquote`` だけではエンティティが
    戻らないので ``base_dir / "a&amp;b.jpg"`` を探すと取りこぼす。
    取りこぼすと幅/高さの焼き込みも
    ``_img_targets`` 登録も飛ばされ、``loadResource`` は Qt が復号済みの
    実パスで呼ばれるためキーが一致せず、GUI スレッドでの原寸同期デコード
    (LRU 予算の外) に落ちる。``html.unescape`` → ``unquote`` の順で
    外側から剥がす。
    """
    return _strip_dot_slash(urllib.parse.unquote(html.unescape(src)))


# Tag paragraphs that contain only a single image so CSS can collapse the
# default top/bottom margin — post.md emits each image as its own paragraph
# (``![](...)\n\n![](...)``) and QTextBrowser's default spacing leaves a
# visible gap between consecutive images.
_IMG_ONLY_P_RE = re.compile(
    r"<p>(\s*<img\b[^>]*>\s*)</p>", re.IGNORECASE
)


def _rewrap_img_paragraphs(body_html: str) -> str:
    """Collapse single-image paragraphs to a tight, flush-top block.

    Pure string work (no Qt) so it can run either synchronously on the
    GUI thread (text-only fast path) or inside ``_render_post_body`` off the
    GUI thread (image posts).
    """

    def _rewrap(m: re.Match[str]) -> str:
        inner = m.group(1)
        # Inject vertical-align:top so the image sits flush with the
        # block top (default baseline alignment leaves a descender gap).
        inner = re.sub(
            r"<img\b", '<img style="vertical-align:top;"', inner, count=1,
        )
        return (
            '<p style="margin:0 0 6px 0;line-height:1;'
            '-qt-block-indent:0;text-indent:0;">'
            f"{inner}</p>"
        )

    return _IMG_ONLY_P_RE.sub(_rewrap, body_html)


def resolve_markdown_image_path(name: str, base_dir: Path) -> Path | None:
    """Resolve an ``<img>`` resource name to a local file under *base_dir*.

    ``name`` is whatever :meth:`QTextImageFormat.name` returns for the
    image under the cursor — typically the URL-encoded relative reference
    baked into the rendered HTML (``./%E7%B5%B5.jpg``), but Qt may hand
    back an absolute ``file:///...`` URL once the document has resolved it
    against ``baseUrl``.  Pure logic (no Qt types) so it's unit-testable
    without a QApplication.

    Returns ``None`` when the name is empty or the resolved path doesn't
    exist as a file — callers should skip the context-menu entry in that
    case rather than emit a signal for a dead path.
    """
    if not name:
        return None
    decoded = urllib.parse.unquote(name)
    # Strip a file:// prefix if present (Qt sometimes returns the fully
    # resolved absolute URL rather than the original relative reference).
    if decoded.startswith("file:"):
        parsed = urllib.parse.urlparse(decoded)
        decoded = urllib.parse.unquote(parsed.path)
        # On Windows, urlparse leaves a leading slash before the drive
        # letter (e.g. "/N:/foo") — strip it.
        if len(decoded) > 2 and decoded[0] == "/" and decoded[2] == ":":
            decoded = decoded[1:]
    candidate = Path(decoded)
    if not candidate.is_absolute():
        candidate = base_dir / _strip_dot_slash(decoded)
    try:
        if candidate.is_file():
            return candidate
    except OSError:
        return None
    return None


# Default body font size in points when ``ViewerState.markdown_font_pt``
# is 0 (application default).  Also the floor/ceiling for Ctrl+wheel zoom.
_DEFAULT_FONT_PT = 11
_MIN_FONT_PT = 8
_MAX_FONT_PT = 32
#: 「アプリ既定」の実値の公開名 — 設定ダイアログの ``markdown_font_pt = 0``
#: と ContentView の適用が同じ 1 つの数字を見るようにする。
DEFAULT_FONT_PT = _DEFAULT_FONT_PT
#: 下限・上限の公開名。設定ダイアログのスピンの範囲はここから導出する —
#: 手書きで写すと、こちら側を広げたときに Ctrl+ホイールで到達した値が
#: 「設定を開いて OK」だけで黙ってクランプされる。
MIN_FONT_PT = _MIN_FONT_PT
MAX_FONT_PT = _MAX_FONT_PT

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body {{ font-family: 'Segoe UI', 'Yu Gothic UI', sans-serif;
       font-size: {font_pt}pt; line-height: 1.6; }}
/* ``height: auto`` would nullify the HTML ``height`` attribute and force
   Qt to compute display height from the 1×1 placeholder's intrinsic
   aspect — which is expensive and wrong.  Keep only ``max-width`` so
   the HTML ``width``/``height`` we inject drives layout directly. */
img {{ max-width: 100%; }}
/* Chrome colours go through ``palette(mid)`` so they track the active
   light/dark theme (a hardcoded grey crushes against the dark Base
   background). */
h1 {{ border-bottom: 1px solid palette(mid); padding-bottom: 4px; }}
h2 {{ border-bottom: 1px solid palette(mid); padding-bottom: 2px; }}
blockquote {{ border-left: 3px solid palette(mid); margin: 0; padding: 4px 12px;
              color: palette(mid); }}
/* code/pre backgrounds: translucent wash of the theme's ``text_muted``
   token, baked in at format time (QTextDocument CSS has no translucent
   palette role — same mechanism as ``a.localpost`` below). */
code {{ background: {code_bg}; padding: 1px 4px;
       border-radius: 3px; }}
pre {{ background: {pre_bg}; padding: 8px;
       border-radius: 4px; overflow-x: auto; }}
hr {{ border: none; border-top: 1px solid palette(mid); }}
/* Downloaded-post links: a body link whose target resolves to a folder we
   already have on disk.  The main anchor still opens the web page; the
   green pill marks it as downloaded, and the trailing 📁 anchor jumps to
   the local copy in the viewer.  The green is the theme's ``success``
   token (QTextDocument CSS has no palette role for it, so it is baked in
   at format time — the template is re-rendered on every show, so theme
   switches are picked up naturally). */
a.localpost {{ color: {localpost_color}; font-weight: 600; text-decoration: none;
            background: {localpost_bg}; padding: 0 3px;
            border-radius: 3px; }}
a.localpost-jump {{ text-decoration: none; }}
/* 投稿メタカード: 生の内部キー羅列の代わりに出す
   日本語ラベル + 整形値の表。ラベル列はヒント色（palette(mid)）。 */
table.metacard {{ margin: 2px 0 10px 0; }}
td.metalabel {{ color: palette(mid); padding-right: 14px; }}
td.metavalue {{ padding-right: 4px; }}
</style></head><body>
{body}
</body></html>
"""


#: 「この .md は投稿メタを持つ post.md か」を決める識別キー。契約キー全体
#: （``META_KEYS_IN_ORDER``）には ``url`` / ``tags`` / ``creator`` / ``plan``
#: といった一般名詞が混ざるので、判定にはそれらを含めない。キー名の正本は
#: ``common.post_meta``（ここは投稿 1 件を指す部分集合の宣言だけ）。
_POST_IDENTITY_KEYS = (_KEY_POST_ID, _KEY_SERVICE, _KEY_POSTED_AT)


def _post_header_html(text: str) -> tuple[str, str]:
    """post.md の先頭（タイトル + 生メタブロック）をメタカード HTML へ。

    ``- posted_at: 2026-01-15T20:30:00+09:00`` のような内部キーと ISO 値を
    そのまま出すと、タイル側の整形表示（``2026-01-15``）と食い違う。ここで
    (ヘッダ HTML, 本文テキスト) に分離し、本文だけを markdown として
    描画する。メタブロックを持たないただの .md は ``("", text)`` を返して
    全文描画。
    """
    from ..common.post_meta import (
        KEY_CREATOR,
        KEY_DOWNLOADED_AT,
        KEY_FAVORITES,
        KEY_LOCKED_CONTENTS,
        KEY_PLAN,
        KEY_PLAN_PRICE,
        KEY_POST_ID,
        KEY_POSTED_AT,
        KEY_SERVICE,
        KEY_TAGS,
        KEY_URL,
    )
    from .post_md import parse_post_md

    parsed = parse_post_md(text)
    # post.md かどうかは「投稿を一意に指す識別キーがあるか」で判定する。中央
    # ペインは拡張子 ``.md`` なら何でもここへ流す（``content_view.show_path``）
    # 一方、``META_LINE_RE`` は ``[a-z_]+`` なら何でもメタ行として飲むので、
    # ``- fixed: …`` のような小文字キーの箇条書きで始まる普通の Markdown
    # （CHANGELOG.md 等）が丸ごとメタカードに化けて本文から消える。契約キー
    # 全体を見る判定では ``url`` / ``tags`` / ``creator`` / ``plan`` といった
    # 一般名詞が残るため、英語の README（``- url: https://…`` を含む箇条書き）
    # が同じ化け方をする。
    # 識別キーは書き手が必ず揃えて出す（公開仕様 docs/formats/post-md.md）
    # ので、投稿以外の .md が偶然 3 つのどれかを持つことはまず無い。
    if not any(key in parsed.meta for key in _POST_IDENTITY_KEYS):
        return "", text
    meta = dict(parsed.meta)
    rows: list[tuple[str, str]] = []

    meta.pop(KEY_POSTED_AT, "")
    if parsed.posted_at is not None:
        dt = parsed.posted_at
        shown = (
            dt.strftime("%Y-%m-%d %H:%M") if (dt.hour or dt.minute)
            else dt.strftime("%Y-%m-%d")
        )
        rows.append(
            (t("viewer.markdown_view.meta_posted"), html.escape(shown))
        )
    creator = meta.pop(KEY_CREATOR, "").strip()
    if creator:
        rows.append(
            (t("viewer.markdown_view.meta_creator"), html.escape(creator))
        )
    plan = meta.pop(KEY_PLAN, "").strip()
    price = meta.pop(KEY_PLAN_PRICE, "").strip()
    if plan or price:
        rows.append((
            t("viewer.markdown_view.meta_plan"),
            html.escape(" ".join(p for p in (plan, price) if p)),
        ))
    meta.pop(KEY_TAGS, "")
    if parsed.tags:
        rows.append((
            t("common.label.tag"),
            html.escape(t("common.sep.comma").join(parsed.tags)),
        ))
    favorites = meta.pop(KEY_FAVORITES, "").strip()
    if favorites:
        # ♡ はラベルではなく値の先頭に付ける（他行と同じくラベル列は純
        # テキストで先頭を揃える）。
        rows.append((
            t("viewer.markdown_view.meta_favorites"),
            t(
                "viewer.markdown_view.meta_favorites_value",
                n=html.escape(favorites),
            ),
        ))
    meta.pop(KEY_LOCKED_CONTENTS, "")
    if parsed.locked_count > 0:
        rows.append((
            t("viewer.markdown_view.meta_locked"),
            t("viewer.markdown_view.meta_locked_value", n=parsed.locked_count),
        ))
    url = meta.pop(KEY_URL, "").strip()
    service = meta.pop(KEY_SERVICE, "").strip()
    post_id = meta.pop(KEY_POST_ID, "").strip()
    if url:
        link_text = " / ".join(x for x in (service, post_id) if x) or url
        rows.append((
            t("viewer.markdown_view.meta_page"),
            f'<a href="{html.escape(url, quote=True)}">'
            f"{html.escape(link_text)}</a>",
        ))
    elif service:
        rows.append(
            (t("viewer.markdown_view.meta_service"), html.escape(service))
        )
    meta.pop(KEY_DOWNLOADED_AT, "")  # 内部管理値 — 表示しない
    # 未知キー（将来の拡張・手書き）は情報を失わないよう素通しで表示。
    for key, value in meta.items():
        if value.strip():
            rows.append((html.escape(key), html.escape(value.strip())))

    header = ""
    if parsed.title:
        header += f"<h1>{html.escape(parsed.title)}</h1>\n"
    if rows:
        cells = "".join(
            f'<tr><td class="metalabel">{label}</td>'
            f'<td class="metavalue">{value}</td></tr>'
            for label, value in rows
        )
        header += f'<table class="metacard">{cells}</table>\n'
    return header, parsed.body


def _theme_style_args() -> dict[str, str]:
    """Theme-derived values for the template's colour placeholders
    (design-token rule: no hardcoded colours — the localpost pill uses the
    ``success`` role, code/pre a translucent ``text_muted`` wash)."""
    tokens = current_tokens()
    return {
        "code_bg": rgba(tokens.text_muted, 0.15),
        "pre_bg": rgba(tokens.text_muted, 0.10),
        "localpost_color": tokens.success,
        "localpost_bg": rgba(tokens.success, 0.15),
    }


def _layout_box(src: QSize, max_w: int, max_px: int) -> QSize:
    """元画像サイズ *src* から表示ボックス（= デコード目標）を導出する。

    幅をコンテンツ幅 *max_w* まで縮め（拡大はしない）、``max_px`` > 0 なら
    デコード画素数がその予算に収まるまでアスペクト比を保って更に縮める
    （予算を超えた QImage は LRU に入れてもらえず QTextDocument に無制限
    常駐する）。

    レンダータスク（焼き込み）とビュー（リフロー・デコード目標）が同じ
    関数を通ることが要点: ``_img_targets`` は**毎回ここから導出し直す**
    派生値であり、初回レンダ時の幅を溜め込まない。縮小済みの表示ボックスを
    保存して ``min(target.width(), max_w)`` で再計算すると、狭いウィンドウで
    開いた投稿はその後ウィンドウを広げても画像が初回幅までしか戻らない。
    """
    src_w, src_h = src.width(), src.height()
    if src_w <= 0 or src_h <= 0:
        return QSize(src_w, src_h)
    if src_w > max_w:
        box = QSize(max_w, max(1, round(src_h * (max_w / src_w))))
    else:
        box = QSize(src_w, src_h)
    if max_px > 0:
        px = box.width() * box.height()
        if px > max_px:
            scale = (max_px / px) ** 0.5
            box = QSize(
                max(1, int(box.width() * scale)),
                max(1, int(box.height() * scale)),
            )
    return box


#: 同期（テキスト専用）レンダの本文長の上限（文字）。これを超える本文は
#: 画像の有無に関わらずワーカーの ``_render_post_body`` へ回す。``markdown_it``
#: のレンダ時間は本文長に対して超線形で、GUI スレッドで走らせるとキャンセル
#: できないフリーズになる（画像入りの経路は元からワーカーで同じ処理をする）。
_TEXT_ONLY_RENDER_MAX_CHARS = 64 * 1024
