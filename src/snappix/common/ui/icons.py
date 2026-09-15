"""Themed SVG icons for the snappix GUIs.

A small, hand-authored set of Lucide-style 24×24 outline glyphs embedded as
strings (no data files → nothing to add to the PyInstaller spec; the
``PySide6.QtSvg`` import below is module-level so PyInstaller traces it).
Icons are tinted with the current theme's tokens at render time.

Usage::

    from snappix.common.ui import set_icon
    set_icon(button, "arrow-left")            # tinted with tokens.text
    set_icon(action, "search", role="muted")  # decorative, tokens.text_muted

:func:`set_icon` registers the target in a weak registry;
``apply_theme`` calls :func:`retint_all` so every registered icon is
re-rendered in the new theme's colours.  Prefer it over calling
:func:`icon` + ``setIcon`` yourself, which would go stale on theme switch
(acceptable only for short-lived widgets, e.g. dialogs).
"""

from __future__ import annotations

import weakref
from typing import Literal

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"'
    ' stroke="currentColor" stroke-width="{stroke_width}" stroke-linecap="round"'
    ' stroke-linejoin="round">{body}</svg>'
)

#: name -> svg body.  Outline strokes inherit ``currentColor``; solid shapes
#: (play/pause/speaker) override with ``fill="currentColor" stroke="none"``.
_ICONS: dict[str, str] = {
    "arrow-left": '<path d="M19 12H5"/><path d="m12 19-7-7 7-7"/>',
    "arrow-right": '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
    "arrow-up": '<path d="M12 19V5"/><path d="m5 12 7-7 7 7"/>',
    "arrow-down": '<path d="M12 5v14"/><path d="m19 12-7 7-7-7"/>',
    "refresh": (
        '<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/>'
        '<path d="M21 3v5h-5"/>'
    ),
    "more-horizontal": (
        '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>'
        '<circle cx="5" cy="12" r="1"/>'
    ),
    "search": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    "help-circle": (
        '<circle cx="12" cy="12" r="10"/>'
        '<path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>'
        '<path d="M12 17h.01"/>'
    ),
    "folder": (
        '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1'
        '-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0'
        ' 2 2Z"/>'
    ),
    "folder-open": (
        '<path d="m6 14 1.45-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94'
        ' 2.5l-1.55 6a2 2 0 0 1-1.94 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1'
        ' 2-2h3.93a2 2 0 0 1 1.66.9l.82 1.2a2 2 0 0 0 1.66.9H18a2 2 0'
        ' 0 1 2 2v2"/>'
    ),
    "external-link": (
        '<path d="M15 3h6v6"/><path d="M10 14 21 3"/>'
        '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1'
        ' 2-2h6"/>'
    ),
    # Lucide folder-output — **OS のファイラを起動する**席の専用図像
    # (UIレビュー 07-25 #56)。folder-open（= アプリ内でそのフォルダを開く:
    # ルート選択・ZIP ドリル）と同じ図像が「エクスプローラで開く」= OS 起動
    # にも流用されており、同じ絵で 4 つの動詞を兼ねていた。フォルダから矢印
    # が**外へ出る**ので「この箱の中身を外のアプリで見る」と読める。
    "folder-output": (
        '<path d="M2 7.5V5a2 2 0 0 1 2-2h3.93a2 2 0 0 1 1.66.9l.82 1.2a2'
        ' 2 0 0 0 1.66.9H20a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2h-7.5"/>'
        '<path d="M10 15H2"/>'
        '<path d="m5 12-3 3 3 3"/>'
    ),
    "play": (
        '<polygon points="6 3 20 12 6 21 6 3" fill="currentColor"'
        ' stroke="none"/>'
    ),
    "pause": (
        '<rect x="6" y="4" width="4" height="16" rx="1" fill="currentColor"'
        ' stroke="none"/>'
        '<rect x="14" y="4" width="4" height="16" rx="1" fill="currentColor"'
        ' stroke="none"/>'
    ),
    "volume": (
        '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"'
        ' fill="currentColor" stroke="none"/>'
        '<path d="M15.5 8.5a5 5 0 0 1 0 7"/>'
        '<path d="M18.5 5.5a9 9 0 0 1 0 13"/>'
    ),
    "volume-x": (
        '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"'
        ' fill="currentColor" stroke="none"/>'
        '<line x1="22" y1="9" x2="16" y2="15"/>'
        '<line x1="16" y1="9" x2="22" y2="15"/>'
    ),
    "repeat": (
        '<path d="m17 2 4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/>'
        '<path d="m7 22-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/>'
    ),
    "x": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    # 印ストリップ（E2）: タグの追加 / 編集の入口。
    "plus": '<path d="M5 12h14"/><path d="M12 5v14"/>',
    "tag": (
        '<path d="M12.586 2.586A2 2 0 0 0 11.172 2H4a2 2 0 0 0-2 2v7.172a2 2 0 0 0 '
        '.586 1.414l8.704 8.704a2.426 2.426 0 0 0 3.42 0l6.58-6.58a2.426 2.426 0 0 0 0-3.42z"/>'
        '<circle cx="7.5" cy="7.5" r=".5" fill="currentColor"/>'
    ),
    # Warning triangle (attention states, e.g. an expired session).
    "alert-triangle": (
        '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0'
        ' 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z"/>'
        '<line x1="12" y1="9" x2="12" y2="13"/>'
        '<line x1="12" y1="17" x2="12.01" y2="17"/>'
    ),
    # Notification bell (notification-history drawer toggle).
    "bell": (
        '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/>'
        '<path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>'
    ),
    # Session sign-out / discard.
    "log-out": (
        '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>'
        '<polyline points="16 17 21 12 16 7"/>'
        '<line x1="21" x2="9" y1="12" y2="12"/>'
    ),
    # Shared with the QSS combobox / spinbox arrow rules (qss.py renders
    # them to files via svg_source since QSS url() cannot embed data URIs).
    # Lucide funnel — the toolbar filter-popover trigger (種別 / 投稿日 /
    # ★ / あとで見る の適応型フィルタ入口).
    "filter": (
        '<polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>'
    ),
    # Lucide arrow-down-up — the toolbar 「並び・表示」 popover trigger
    # (UIレビュー 07-25 #100: the three toolbar popovers are icon-only + tooltip;
    # #24 renamed this one).  A sort glyph, deliberately unlike the funnel
    # (フィルタ), the three dots (⋯) and the three pane-toggle panels.
    "sort-display": (
        '<path d="m3 16 4 4 4-4"/>'
        '<path d="M7 20V4"/>'
        '<path d="m21 8-4-4-4 4"/>'
        '<path d="M17 4v16"/>'
    ),
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "chevron-up": '<path d="m18 15-6-6-6 6"/>',
    # Lucide check — the QSS-generated tick inside a checked QCheckBox
    # indicator (UIレビュー 2026-08-28 N-54).  Styling the indicator makes Qt
    # stop drawing the native mark, so the glyph has to come from here (the
    # one home for glyph paths) via ``qss.py::_glyph_url``.
    "check": '<path d="M20 6 9 17l-5-5"/>',
    # The radio counterpart of ``check``: a filled dot.  A radio's selected
    # state must not read as a tick — that is the checkbox's mark, and the
    # two controls mean different things (one of many vs any number).
    "radio-dot": '<circle cx="12" cy="12" r="5" fill="currentColor" stroke="none"/>',
    # The partially-checked mark for a tri-state indicator.  Without it the
    # ``PartiallyChecked`` state falls through to the plain rule and is pixel
    # identical to unchecked.
    "minus": '<path d="M6 12h12"/>',
    # Compact prev/next steppers (e.g. the PDF page-navigation toolbar) —
    # lighter than the full arrow-left/right glyphs.
    "chevron-left": '<path d="m15 18-6-6 6-6"/>',
    "chevron-right": '<path d="m9 18 6-6-6-6"/>',
    # Empty-state card regularisation (redesign 2026-07 Phase 3-4):
    # bookmark-manager dialog's empty-list card.
    "bookmark": (
        '<path d="m19 21-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16z"/>'
    ),
    # Lucide columns-2 — the preview header's 「◧ 分割に戻す」 exit button
    # (split-view layout redesign 2026-07: leave preview-maximised back to
    # the [grid | preview] split).
    "columns": (
        '<rect width="18" height="18" x="3" y="3" rx="2"/>'
        '<path d="M12 3v18"/>'
    ),
    # Lucide maximize — the preview header's 「⤢ 最大化 (E)」 entry button
    # (UIレビュー 07-25 #16: 分割ビューから最大化へ入る可視導線).  Four corner
    # brackets = "この席を広げる"; deliberately distinct from external-link
    # (アプリ外で開く) and columns (分割へ戻す).
    "maximize": (
        '<path d="M8 3H5a2 2 0 0 0-2 2v3"/>'
        '<path d="M21 8V5a2 2 0 0 0-2-2h-3"/>'
        '<path d="M3 16v3a2 2 0 0 0 2 2h3"/>'
        '<path d="M16 21h3a2 2 0 0 0 2-2v-3"/>'
    ),
    # Lucide expand — 4 方向へ開く矢印。**アプリ内の全画面**（閲覧モード）
    # 専用の図像 (UIレビュー 07-25 #104): 以前は external-link を流用しており、
    # 同じ図像が「既定アプリで開く（アプリ外へ出る）」と「全画面（アプリ内に
    # 留まる）」という正反対の意味を兼ねていた。maximize（角括弧 = 席を広げる）
    # とも別物として読めるよう、矢印で「画面いっぱいに開く」を表す。
    "expand": (
        '<path d="m21 21-6-6m6 6v-4.8m0 4.8h-4.8"/>'
        '<path d="M3 16.2V21m0 0h4.8M3 21l6-6"/>'
        '<path d="M21 7.8V3m0 0h-4.8M21 3l-6 6"/>'
        '<path d="M3 7.8V3m0 0h4.8M3 3l6 6"/>'
    ),
    # Pane visibility toggles (折り畳み導線 2026-07) — the unified toolbar's
    # right-end button group (F7 / F6 / F8).  Same 24×24 rect scaffold as
    # "columns" so the family reads as one set, and — since the redesign
    # (UIレビュー 07-25 #77) — **all three paint the seat they toggle**:
    # 左列 = ナビレール / 中央帯 = プレビュー列 / 右列 = 情報パネル。
    # 以前は左右が「仕切り線の位置が 6px 違うだけ」で判別できず、中央だけが
    # 塗り規則から外れて**右半分**を塗っていた（「右ペイン」と誤読される
    # panel-right との衝突）。塗りは矩形の枠に密着させる（内側 2px の
    # インセットを廃止 — 席そのものに見えるように）。
    "panel-left": (
        '<rect width="18" height="18" x="3" y="3" rx="2"/>'
        '<path d="M9 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h4z"'
        ' fill="currentColor" stroke="none"/>'
        '<path d="M9 3v18"/>'
    ),
    "panel-right": (
        '<rect width="18" height="18" x="3" y="3" rx="2"/>'
        '<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4z"'
        ' fill="currentColor" stroke="none"/>'
        '<path d="M15 3v18"/>'
    ),
    "panel-preview": (
        '<rect width="18" height="18" x="3" y="3" rx="2"/>'
        '<rect width="6" height="18" x="9" y="3"'
        ' fill="currentColor" stroke="none"/>'
        '<path d="M9 3v18"/>'
        '<path d="M15 3v18"/>'
    ),
    # 「フィット ⇄ 実寸」トグル（画像プレビューの操作カプセル / ライトボックス）。
    # **ズームの語彙**であって「席を広げる」語彙ではない (UIレビュー 08-28 N-46):
    # 以前は maximize と**同型の四隅の角括弧**で、分割ビューでカプセルとステージ
    # ヘッダーの「⤢ 最大化 (E)」が同じ絵で別の意味を持っていた。外枠の中に小さな
    # 矩形＝「画像を枠に収める」。塗り・全高の仕切り線を持たないので、席そのものを
    # 塗る panel-left/right/preview + columns の一族とも読み違えない。
    "fit-frame": (
        '<rect width="18" height="18" x="3" y="3" rx="2"/>'
        '<rect width="10" height="7" x="7" y="8.5" rx="1"/>'
    ),
    # タイルホバーの「類似画像を検索」ボタン (UIレビュー 08-28 N-63)。以前は
    # ``drawText`` の記号文字 ◇ (U+25C7) をベタ書きしており、♡ と同じ
    # フォント欠落リスク（_indicator.py の注記参照）を負っていた。ずらして
    # 重ねた 2 つの菱形 = 「これに似たもの」— 既存の ◇ の比喩を保ったまま
    # 図像化する。
    "similar": (
        '<path d="m9 3 5 5-5 5-5-5Z"/>'
        '<path d="m15 11 5 5-5 5-5-5Z"/>'
    ),
    # Lucide settings — 「管理ダイアログを開く」単一アクションの席
    # (UIレビュー 08-28 N-24)。ナビレールのセクション見出しはメニューでは
    # なく管理ダイアログを開くだけなので、⋯（＝そのペインの表示オプション）
    # を流用してはいけない。
    "settings": (
        '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2'
        ' 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73'
        ' 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0'
        ' 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2'
        ' 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1'
        ' 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2'
        ' 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15'
        '-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0'
        ' 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/>'
        '<circle cx="12" cy="12" r="3"/>'
    ),
    # Lucide puzzle — プラグイン管理ダイアログの空状態カード
    # (UIレビュー 07-25 #126)。以前は sliders を流用しており、ツールバーの
    # 検索オプション（sliders = つまみを調整する）と図像が二重使用だった。
    "puzzle": (
        '<path d="M15.39 4.39a1 1 0 0 0 1.68-.474 2.5 2.5 0 1 1 3.014'
        ' 3.015 1 1 0 0 0-.474 1.68l1.683 1.682a2.414 2.414 0 0 1 0'
        ' 3.414L19.61 15.39a1 1 0 0 1-1.68-.474 2.5 2.5 0 1 0-3.014'
        ' 3.015 1 1 0 0 1 .474 1.68l-1.683 1.682a2.414 2.414 0 0 1-3.414'
        ' 0L8.61 19.61a1 1 0 0 0-1.68.474 2.5 2.5 0 1 1-3.014-3.015 1 1 0'
        ' 0 0 .474-1.68l-1.683-1.682a2.414 2.414 0 0 1 0-3.414L4.39'
        ' 8.61a1 1 0 0 1 1.68.474 2.5 2.5 0 1 0 3.014-3.015 1 1 0 0'
        ' 1-.474-1.68z"/>'
    ),
}


def register_icons(mapping: dict[str, str]) -> None:
    """Add tool-/plugin-provided glyphs to the icon set.

    *mapping* is ``name -> svg body`` in the same Lucide-style 24×24 outline
    format as the built-in set (strokes inherit ``currentColor``).  A name
    that is already registered **with a different body** raises
    ``ValueError`` so a plugin cannot silently repaint a built-in glyph.
    Registered icons get the same caching / theme-retint behaviour as the
    built-ins.

    **Re-registering an identical body is a no-op** (``qss.py``'s
    ``register_qss_fragment`` #121 と同型 — #185): プラグインのセッション内
    無効化 → 再有効化はホストの ``_purge_modules`` がモジュールを破棄して
    再 import + activate 再実行になるため、無条件拒否だと 2 サイクル目に
    「自分が 1 サイクル目に登録した名前」と衝突して activate が恒久失敗する。
    同一内容の再登録を許しても、組み込み / 他プラグインのグリフを**別の**
    図形で塗り替えることは依然として拒否される。

    The call is **atomic**: every name is checked before anything is
    inserted, so a rejected mapping leaves the registry untouched (review
    #115).  Otherwise a plugin whose ``activate()`` failed on a colliding
    name would leave its earlier glyphs behind and collide with *itself* on
    the next attempt.
    """
    clashes = [
        name
        for name, body in mapping.items()
        if name in _ICONS and _ICONS[name] != body
    ]
    if clashes:
        raise ValueError(
            f"icon name {clashes[0]!r} is already registered"
            if len(clashes) == 1
            else f"icon names {sorted(clashes)!r} are already registered"
        )
    _ICONS.update(mapping)

#: Colour roles an icon can be tinted with (keys of ``_ROLE_TO_TOKEN``).
IconRole = Literal["text", "muted", "accent", "danger", "on-accent"]

#: Which token a colour role reads (resolved at render time).
_ROLE_TO_TOKEN: dict[str, str] = {
    "text": "text",
    "muted": "text_muted",
    "accent": "accent",
    "danger": "danger",
    "on-accent": "text_on_accent",
}

#: Themed :func:`icon` results.  The key carries **every** colour baked into
#: the QIcon — the role colour *and* the disabled colour — because two themes
#: can share a role colour while differing in ``text_disabled`` (the built-in
#: washi / linen pair does), and a key missing the second one hands the new
#: theme the old theme's Disabled artwork.
_cache: dict[tuple[str, str, str, int], QIcon] = {}

#: Fixed-colour :func:`fixed_pixmap` results.  The colour is explicit (never
#: theme-derived), so nothing invalidates these.  They are rasterised from
#: inside ``paintEvent`` for every visible tile chip, which is the whole
#: reason the cache exists: without it each frame re-parses the SVG.
_fixed_cache: dict[tuple[str, str, int, float], QPixmap] = {}

#: Weak registry of targets whose icons must re-render on theme switch.
#: value = (icon name, logical px size, colour role).
_applied: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

#: Same, for targets that take a **QPixmap** instead of a QIcon (QLabel).
#: Kept separate because the refresh call differs (``setPixmap``).
_applied_pixmaps: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _role_color(role: str) -> str:
    from .theme import current_tokens  # runtime import; theme imports us

    try:
        token_name = _ROLE_TO_TOKEN[role]
    except KeyError:
        raise ValueError(
            f"unknown icon role {role!r}; expected one of "
            f"{sorted(_ROLE_TO_TOKEN)}"
        ) from None
    return getattr(current_tokens(), token_name)


def svg_source(name: str, color: str, *, stroke_width: float = 2.0) -> str:
    """The full SVG source for glyph *name* tinted *color*.

    Shared by :func:`icon` (via ``_render``) and the QSS chevron generation
    (``qss.py`` writes the string to a file because QSS ``url()`` cannot
    embed data URIs) so the glyph set and the SVG scaffold live in exactly
    one place.  ``stroke_width`` matches the icon default (2) unless the
    consumer needs a heavier stroke for small render sizes.
    """
    if name not in _ICONS:
        raise ValueError(
            f"unknown icon name {name!r}; available: {sorted(_ICONS)}"
        )
    svg = _TEMPLATE.format(body=_ICONS[name], stroke_width=stroke_width)
    return svg.replace("currentColor", color)


def _render(name: str, color: str, size: int, dpr: float) -> QPixmap:
    """Rasterise glyph *name* tinted *color* at *size* × *dpr*.

    Goes through :func:`svg_source` so the scaffold really does live in one
    place — a second inline ``_TEMPLATE.format`` here would let the QSS chevron
    and the QIcon path drift apart the moment either gains a tweak (#187).
    :func:`fixed_pixmap` delegates here too, so the rasterisation (dpr rounding
    included) has exactly one implementation.
    """
    renderer = QSvgRenderer(QByteArray(svg_source(name, color).encode("utf-8")))
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def _dim_pixmap(pm: QPixmap, alpha: int) -> QPixmap:
    """The same artwork drawn at *alpha* (0–255) — 「1 段落とす」 for icons.

    Composited rather than re-rendered from a translucent SVG colour: Qt's SVG
    parser has no portable functional ``rgba()`` notation, and re-rasterising
    would give the Disabled variant its own copy of the render path.  The
    target is built at the source's PHYSICAL size and the dpr re-applied
    afterwards, so a 2x pixmap stays 2x.
    """
    out = QPixmap(pm.size())
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.setOpacity(max(0.0, min(1.0, alpha / 255.0)))
    painter.drawPixmap(out.rect(), pm)
    painter.end()
    out.setDevicePixelRatio(pm.devicePixelRatio())
    return out


def fixed_pixmap(name: str, color: str, *, size: int = 18, dpr: float = 1.0) -> QPixmap:
    """Rasterise glyph *name* in an explicit, **theme-independent** *color*.

    For chrome that sits ON TOP OF image content — the preview control capsule,
    the lightbox bar, the tile hover overlays — which keeps a fixed dark scrim
    and light glyphs regardless of theme (docs/claude/design.md 使用ルール 2).
    Those surfaces must not go through :func:`icon`, whose colour comes from the
    theme tokens (a light theme would paint them dark on a dark scrim).

    Callers pass the colour from ``common/ui/overlay.py`` (the single fixed
    overlay palette) — this function deliberately takes no role, so no literal
    colour can enter here.  **GUI thread only** (QPixmap).

    The rasterisation itself is :func:`_render`'s — this function only fixes
    the *colour policy* (explicit instead of role-derived), so the two paths
    cannot drift in their dpr handling.  Results are cached: the colour is
    explicit, so no theme switch can invalidate them, and the combinations
    are bounded by the overlay palette × the chip sizes.
    """
    key = (name, color, size, dpr)
    cached = _fixed_cache.get(key)
    if cached is None:
        cached = _render(name, color, size, dpr)
        _fixed_cache[key] = cached
    return cached


def fixed_icon(name: str, color: str, *, size: int = 18) -> QIcon:
    """A 1x/2x :class:`QIcon` of glyph *name* in a fixed, theme-independent colour.

    The QIcon counterpart of :func:`fixed_pixmap` — one implementation shared by
    every image-overlay surface (``image_view`` の操作カプセル / ``lightbox`` の
    上部バー).  Both used to carry their own copy of this loop **plus their own
    glyph table**, which is how the capsule's 「フィット」 glyph drifted into being
    the same four corner brackets as ``maximize`` (UIレビュー 08-28 N-46) —
    the glyph set now lives in ``_ICONS`` only.  **GUI thread only**.

    A ``QIcon.Mode.Disabled`` pixmap is registered alongside the normal one —
    overlay chrome cannot express 「無効」 through the theme's ``text_disabled``
    (it rides a fixed dark scrim), so it uses the overlay palette's own dim
    step (``overlay.OVERLAY_TEXT_DIM``'s alpha).  Without a Disabled pixmap Qt
    falls back to its own greying of the normal artwork, which on a dark scrim
    is barely a change at all.
    """
    from .overlay import OVERLAY_TEXT_DIM  # local: overlay is a leaf module

    ic = QIcon()
    for dpr in (1.0, 2.0):
        normal = fixed_pixmap(name, color, size=size, dpr=dpr)
        ic.addPixmap(normal)
        ic.addPixmap(_dim_pixmap(normal, OVERLAY_TEXT_DIM.alpha()), QIcon.Mode.Disabled)
    return ic


def icon(name: str, *, role: IconRole = "text", size: int = 16) -> QIcon:
    """A theme-tinted QIcon for *name* (see ``_ICONS`` for the set).

    Raises ``ValueError`` (with the available names) for an unknown *name*
    or *role*, so a typo surfaces at the call rather than as a deferred
    ``KeyError`` when the button first paints.

    A ``QIcon.Mode.Disabled`` variant tinted ``text_disabled`` is registered
    too, so a disabled button's glyph drops to the same step the theme uses
    for disabled *text* instead of Qt's generic greying — which left a
    disabled toolbar button reading as enabled.  Since :func:`set_icon` funnels
    every long-lived button and QAction through here, that applies app-wide.
    """
    if name not in _ICONS:
        raise ValueError(
            f"unknown icon name {name!r}; available: {sorted(_ICONS)}"
        )
    from .theme import current_tokens  # runtime import; theme imports us

    color = _role_color(role)
    # Resolved before the lookup: the Disabled pixmap is baked into the same
    # QIcon, so it belongs in the key (see ``_cache``).
    disabled_color = current_tokens().text_disabled
    key = (name, color, disabled_color, size)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    ic = QIcon()
    # 1x + 2x pixmaps so high-DPI screens get a crisp render.
    for dpr in (1.0, 2.0):
        ic.addPixmap(_render(name, color, size, dpr))
        ic.addPixmap(
            _render(name, disabled_color, size, dpr), QIcon.Mode.Disabled
        )
    _cache[key] = ic
    return ic


def set_icon(target, name: str, *, role: IconRole = "text", size: int = 16) -> None:
    """Set a themed icon on *target* and keep it themed across switches.

    *target* is anything with ``setIcon`` (QAbstractButton, QAction, …).
    Buttons also get ``setIconSize`` so the glyph renders at its intended
    logical size.
    """
    target.setIcon(icon(name, role=role, size=size))
    if hasattr(target, "setIconSize"):
        target.setIconSize(QSize(size, size))
    _applied[target] = (name, size, role)


def set_icon_pixmap(
    label, name: str, *, role: IconRole = "text", size: int = 16
) -> None:
    """Paint a themed icon into *label* as a pixmap, themed across switches.

    The QIcon path (:func:`set_icon`) needs a ``setIcon`` target, so widgets
    that show a glyph as plain artwork — ``QLabel``, e.g. ``EmptyStateCard``'s
    icon — used to bake the creation-time colour into a pixmap and stayed that
    colour after a theme switch (review #118).  This registers them alongside
    the icon targets so ``retint_all`` re-renders them too.
    """
    label.setPixmap(icon(name, role=role, size=size).pixmap(size, size))
    _applied_pixmaps[label] = (name, size, role)


def retint_all() -> None:
    """Re-render every registered icon with the current tokens.

    Called by ``apply_theme``.  Dead Qt objects whose Python wrapper is
    still alive raise ``RuntimeError`` on access — drop them silently.
    """
    for target, (name, size, role) in list(_applied.items()):
        try:
            target.setIcon(icon(name, role=role, size=size))
        except RuntimeError:  # C++ object already deleted
            _applied.pop(target, None)
    for label, (name, size, role) in list(_applied_pixmaps.items()):
        try:
            label.setPixmap(icon(name, role=role, size=size).pixmap(size, size))
        except RuntimeError:  # C++ object already deleted
            _applied_pixmaps.pop(label, None)


__all__ = [
    "fixed_icon",
    "fixed_pixmap",
    "icon",
    "register_icons",
    "retint_all",
    "set_icon",
    "set_icon_pixmap",
    "svg_source",
]
