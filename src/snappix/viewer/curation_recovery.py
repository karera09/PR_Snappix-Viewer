"""横断キュレーション一覧の到達不能行の可視化と張り替え導線。

``resolve_curation_paths`` が読めずに落とした行（確実に消えた ``missing`` /
読めなかった ``unreadable``）は、黙って消すと件数行の数字がズレることでしか
気付けない — しかもドライブレター⇄UNC の付け替え (a) や post.md を持たない
フォルダのリネーム (b) で消えた行は、postref 頼みの ``resolve_moved_entries``
では救えない。

ここはその行を**プレースホルダタイル**として一覧末尾に出し、右クリックの
「現在の場所を指定…」→ :meth:`~snappix.viewer.user_meta.UserMetaStore.rebind_path`
で現在のパスへ張り替えるための部品を提供する。``post_grid.py`` は既に抽出対象
規模なので、新規ロジックはここへ置き、post_grid からは
薄く呼ぶだけにする。

設計上の約束:

* **追加 I/O ゼロ** — 材料は resolve が既に確定させた失われたパスの列だけ。
  プレースホルダは ``thumbnail_resolved=True`` / ``thumbnail_path=None`` /
  ``metadata_loaded=True`` で組むので、サムネイルローダーの遅延 scandir も
  post.md の後追い解決も一切走らない（到達不能なパスに向けた同期 stat を
  GUI 経路に持ち込まない）。画像拡張子を持つ
  ファイル由来のゴーストだけは 3 点セットで塞げない経路が残る —
  ``ChildrenGrid._aspect_source`` は「is_dir=False + 画像拡張子」で
  entry.path 自身を返すため、そのままではアスペクトプローブが死んだパスを
  開きに行き、プール枠を OS タイムアウトぶん占有する。これは
  ``PostGrid._aspect_source`` のオーバーライド（ゴーストは ``None``）が
  塞ぐ。描画は GalleryView の既存プレースホルダパネル（C03 の確定側）に
  乗るので、専用の描画コードもハードコード色も増えない。
* ファイル / フォルダの判別は**綴りだけの純推定**（:func:`looks_like_file`、
  stat しない）。外れると張り替えダイアログの種類が逆になり、正しい種類の
  実体を選ぶ手段が無くなる（ファイル選択ではフォルダを選べない）— だから
  「``.`` があれば拡張子」とは見なさず、拡張子らしい末尾だけをファイルとする。
* **一括の張り替えは行単位の張り替えと同じ材料で組む**。失敗の単位は
  ボリューム（ドライブレターの付け替え・ライブラリごとの移動）なので、
  行単位だけでは数百回のモーダル往復になる。:func:`prefix_rebind_group` が
  ゴースト集合から共通の根を純パス演算で割り出し、:func:`plan_prefix_rebind`
  が「動く件数」と「**併合が起きる件数**」をメモリ上の地図だけで数え、
  :func:`confirm_prefix_rebind` がそれを見せてから
  :meth:`~snappix.viewer.user_meta.UserMetaStore.rebind_prefix` へ渡す。
  動くのは**渡した行だけ**（根の下を走査して当たった行を巻き込まない）—
  対象はゴースト集合、つまり解決が到達不能と確定させた行に限る。
"""

from __future__ import annotations

import os
from pathlib import Path

from ..common.i18n import t
from .folder_scan import (
    ARCHIVE_SUFFIXES,
    DOCUMENT_SUFFIXES,
    IMAGE_SUFFIXES,
    MEDIA_SUFFIXES,
    FolderEntry,
)
from .user_meta import (
    CurationMap,
    UserMeta,
    UserMetaStore,
    absolute_spelling,
    normalize_entry_key,
    rebase_spelling,
)


# ビューアが種別を知っている拡張子。ここに当たる末尾は形に関わらずファイル。
_KNOWN_FILE_SUFFIXES = (
    IMAGE_SUFFIXES | MEDIA_SUFFIXES | ARCHIVE_SUFFIXES | DOCUMENT_SUFFIXES
)
# 未知の拡張子を「拡張子らしい」と見なす最大長（``.`` を除く）。
_MAX_UNKNOWN_SUFFIX_LEN = 5


def looks_like_file(path: Path) -> bool:
    """末尾要素が拡張子らしい末尾を持つならファイルと推定する（純文字列・stat なし）。

    到達不能なパスの実体種別は確かめようがないので、綴りから推定するしかない。
    ``Path.suffix`` の真偽だけで決めると、投稿フォルダ名に頻出する
    ``Vol.2`` / ``2024.01.02`` / ``ver1.5`` / ``Ch.3 update`` が全てファイル扱いに
    なり、張り替えダイアログがファイル選択になってフォルダを選べなくなる。

    判定: ビューアが知っている拡張子（画像・動画・音声・書庫・文書）なら
    ファイル。それ以外は「ASCII 英数字だけで 1〜5 文字、英字を 1 つ以上含む」
    末尾だけをファイルとし、数字だけ（``.2`` / ``.02``）・空白入り・長いものは
    フォルダ名の一部と見なす。
    """
    suffix = path.suffix
    if not suffix:
        return False
    if suffix.lower() in _KNOWN_FILE_SUFFIXES:
        return True
    body = suffix[1:]
    return (
        0 < len(body) <= _MAX_UNKNOWN_SUFFIX_LEN
        and body.isascii()
        and body.isalnum()
        and any(c.isalpha() for c in body)
    )


def build_ghost_entries(
    missing_paths: tuple[str, ...] | list[str],
    unreadable_paths: tuple[str, ...] | list[str],
    library_bases: list[tuple[Path, str]] | None = None,
) -> tuple[list[FolderEntry], dict[str, str], set[str]]:
    """落ちた行のプレースホルダ ``FolderEntry`` 列・キャプション表・
    **破壊的操作を許す鍵の集合**を組む。

    Qt 非依存・I/O ゼロ。返るキャプション表は ``PostGrid._curation_rel_paths``
    へそのまま合流させる — ``_caption_for`` は同じ dict を頭書きに使うので、
    専用の描画分岐を足さずに「どこの何か（見つかりません / 読み取れません）」が
    タイル下に出る。エントリは母集合（``_curation_entries``）とは別に保持し、
    並べ替え・絞り込みの外で常に末尾へ付ける（呼び出し側の
    ``_apply_overlay_population`` 参照）。

    3 つ目の戻り値は **missing（確実に消えた行）だけ**の鍵集合。
    「この一覧から外す」は無確認で★/あとで見る/タグを恒久削除するので、
    *unreadable*（読めなかっただけで実体は生きているかもしれない行 —
    共有が一時的に落ちている / 権限が外れている）へは出してはいけない。

    キャプションの名前は *library_bases* を基準にした相対パス
    (:func:`~snappix.viewer.user_meta_parts.resolve._curation_display_name`) —
    生きている
    行のキャプションと同じ規則なので、旧パスの手掛かりが basename だけに
    落ちない。純パス演算なので「追加 I/O ゼロ」の約束は保たれる。
    """
    from .user_meta import _curation_display_name

    entries: list[FolderEntry] = []
    captions: dict[str, str] = {}
    missing_keys: set[str] = set()
    for group, key, removable in (
        (missing_paths, "viewer.post_grid.curation_ghost_missing", True),
        (unreadable_paths, "viewer.post_grid.curation_ghost_unreadable", False),
    ):
        for p in group:
            path = Path(p)
            entries.append(
                FolderEntry(
                    path=path,
                    title=path.name or str(path),
                    has_post_md=False,
                    # 確定プレースホルダ描画に落とすための 3 点セット:
                    # 解決済み・ソース無し・メタ読込済み（上の docstring 参照）。
                    thumbnail_path=None,
                    thumbnail_resolved=True,
                    metadata_loaded=True,
                    mtime=0.0,
                    is_dir=not looks_like_file(path),
                    size=0,
                )
            )
            if removable:
                missing_keys.add(str(path))
            captions[str(path)] = t(
                key, name=_curation_display_name(path, library_bases),
            )
    return entries, captions, missing_keys


def prefix_rebind_group(
    ghost_keys: "set[str] | frozenset[str]", sample: Path | str,
    library_bases: "list[tuple[Path, str]] | None" = None,
) -> tuple[str, tuple[str, ...]] | None:
    """*sample* と**同じ根を共有するゴースト**の (共通の根, 対象の綴り) 。

    ボリュームごと落ちた失敗を 1 回の指定で直すための材料を、``OverlayList``
    が既に持っているゴースト集合だけから組む（**追加 I/O ゼロ** — この
    モジュールの約束どおり、到達不能なパスへ stat を撃たない）。

    * まず *sample* と**アンカーが同じ**行だけに絞る（``Z:\\`` の行と
      ``\\\\nas2\\share`` の行が 1 回の指定で動いてはいけない — 共通接頭辞を
      素で取ると根が空に落ちて全部が対象になってしまう）。
    * 次に**パス要素単位**の最長共通接頭辞を取る。要素単位なので
      ``Z:\\lib`` が ``Z:\\library`` の根と読まれることがない。畳み方は
      :func:`~snappix.viewer.user_meta.normalize_entry_key` と同じ
      ``os.path.normcase``。

    * 共通接頭辞には**下限**がある。*sample* が登録ライブラリの配下なら
      その**ライブラリの根より浅くならない**（同じライブラリのゴーストだけが
      仲間）。ライブラリの外なら「アンカー + 1 要素」が下限で、同じドライブに
      無関係なゴーストが 1 件残っているだけで根がドライブ根まで崩れ、
      「同じ場所にあった」が事実でない行まで動く形を構造的に閉じる（下限を
      満たさないゴーストは仲間に数えない）。

    2 件未満なら ``None`` — 1 件は既存の行単位の張り替えが正しい手段で、
    一括の確認モーダルを挟む意味が無い。根の綴りは *sample* 側から取るので、
    利用者が右クリックした行と同じ表記で名乗る。
    """
    from ..common.fsutil import pick_library_base

    sample_abs = Path(absolute_spelling(sample))
    sample_parts = sample_abs.parts
    if not sample_parts:
        return None
    anchor = os.path.normcase(sample_parts[0])
    picked = pick_library_base(sample_abs, library_bases) if library_bases else None
    # 下限: ライブラリ配下ならライブラリの根の深さ（ドライブ根を登録して
    # いれば 1 = ボリュームごとの一括が成立する）、外ならアンカー + 1 要素。
    floor = len(Path(picked[0]).parts) if picked is not None else 2
    floor = max(1, min(floor, len(sample_parts)))

    def _shared(parts: tuple[str, ...]) -> int:
        shared = 0
        while (
            shared < len(sample_parts)
            and shared < len(parts)
            and os.path.normcase(parts[shared])
            == os.path.normcase(sample_parts[shared])
        ):
            shared += 1
        return shared

    members: list[tuple[str, int]] = []
    for key in ghost_keys:
        parts = Path(absolute_spelling(key)).parts
        if not parts or os.path.normcase(parts[0]) != anchor:
            continue
        shared = _shared(parts)
        if shared < floor:
            continue
        if picked is None and library_bases and pick_library_base(
            Path(absolute_spelling(key)), library_bases,
        ) is not None:
            # sample がライブラリの外なら、登録ライブラリの中のゴーストは
            # 巻き込まない（そちらはライブラリ根で束ねる別の一括の仲間）。
            continue
        members.append((key, shared))
    if len(members) < 2:
        return None
    common = min(shared for _key, shared in members)
    base = str(Path(*sample_parts[:common]))
    return base, tuple(key for key, _shared in members)


def plan_prefix_rebind(
    members: "tuple[str, ...] | list[str]",
    old_base: Path | str,
    new_base: Path | str,
    meta_map: CurationMap,
) -> tuple[list[str], int]:
    """一括張り替えの (実際に動く行, **併合が起きる件数**) を先に数える。

    確認モーダルは「何件動くか」だけでなく「そのうち何件が行き先の既存の印を
    **不可逆に**巻き込むか」を出さなければならない（併合は取り消せない）。
    数える材料はメモリ上の :class:`~snappix.viewer.user_meta.CurationMap`
    だけなので I/O ゼロ。綴り算術は
    :func:`~snappix.viewer.user_meta.rebase_spelling` = ストアが実際に使う
    のと同じ 1 実装で、件数と実際に動く行がズレない。

    同じ一括の中の別の行が行き先になっているケースは併合に数えない — その行は
    先に退くので、残るのは移動であって既存の印の巻き込みではない。
    """
    member_keys = {normalize_entry_key(m) for m in members}
    targets: list[str] = []
    merges = 0
    for m in members:
        new_spelling = rebase_spelling(m, old_base, new_base)
        if not new_spelling:
            continue
        targets.append(m)
        if normalize_entry_key(new_spelling) in member_keys:
            continue
        existing = meta_map.get(new_spelling)
        if existing is not None and not existing.is_empty():
            merges += 1
    return targets, merges


def prompt_rebind_prefix(parent, old_base: Path) -> str | None:
    """一括張り替えの「移った先のフォルダ」を 1 回だけ選ばせる。

    行単位の :func:`prompt_current_location` と違い、対象は常にフォルダ
    （根の付け替えなので、選ばせるのは根だけ）。キャンセルは ``None``。
    Qt 層はこの関数に閉じる — テストはここを差し替えるだけで通せる。
    """
    from .dialogs import pick_existing_directory

    picked = pick_existing_directory(
        parent,
        t(
            "viewer.post_grid.curation_rebind_prefix_pick",
            name=old_base.name or str(old_base),
        ),
    )
    return picked or None


def confirm_prefix_rebind(
    parent, old_base: Path, new_base: Path, count: int, merges: int,
) -> bool:
    """一括張り替えの確認。``True`` = 実行。

    :func:`confirm_rebind_merge` と同形（動詞ラベル・既定はキャンセル・
    ``destructive``）だが、見せるのは 1 対 1 の中身ではなく**規模**:
    どこからどこへ・何件が動き・そのうち何件が行き先の既存の印と併合される
    か。併合は取り消せないので、0 件のときも件数を明示する（「0 件」と書いて
    あることが「巻き込みは無い」の唯一の根拠になる）。
    """
    from ..common.ui import confirm_action

    return confirm_action(
        parent,
        title=t("viewer.post_grid.curation_rebind_prefix_title"),
        body=t(
            "viewer.post_grid.curation_rebind_prefix_body",
            old=str(old_base), new=str(new_base), n=count, merges=merges,
        ),
        accept_text=t("viewer.post_grid.curation_rebind_prefix_accept"),
        destructive=True,
        # 本文にフォルダ名（自由入力）が埋まる — AutoText の HTML 解釈に
        # 乗せない（:func:`confirm_rebind_merge` と同じ理由）。
        plain_text=True,
    )


def prompt_current_location(parent, old_path: Path) -> str | None:
    """「現在の場所を指定…」のファイルダイアログを開き、選択パスを返す。

    行がフォルダ由来かファイル由来かを :func:`looks_like_file` で推定して
    ``pick_existing_directory`` / ``pick_open_file`` を出し分ける（判別できない
    ときはフォルダ選択）。キャンセルは ``None``。Qt 層はこの関数に閉じる —
    テストはここを差し替えるだけで張り替えフローを通せる。
    """
    from .dialogs import pick_existing_directory, pick_open_file

    name = old_path.name or str(old_path)
    if looks_like_file(old_path):
        picked = pick_open_file(
            parent, t("viewer.post_grid.curation_rebind_pick_file", name=name),
        )
        return picked or None
    picked = pick_existing_directory(
        parent, t("viewer.post_grid.curation_rebind_pick_folder", name=name),
    )
    return picked or None


def _meta_summary(meta: UserMeta) -> str:
    """★ / あとで見る / タグを 1 行に要約する（確認ダイアログ本文用・I/O ゼロ）。

    ★の字面はスターメニュー（``context_menus._add_star_submenu`` の ``"★" * n``）と
    同じ表現。全部空なら「なし」相当の文言を返す — 呼び出し側の空ガードを
    すり抜けても本文が欠けない防御。
    """
    parts: list[str] = []
    if meta.star:
        parts.append("★" * meta.star)
    if meta.later:
        parts.append(t("viewer.common.tooltip_later"))
    if meta.tags:
        parts.append(
            t(
                "viewer.common.tooltip_user_tags",
                tags=t("common.sep.comma").join(meta.tags),
            )
        )
    if not parts:
        return t("viewer.post_grid.curation_rebind_merge_none")
    return t("common.sep.middot").join(parts)


def confirm_rebind_merge(
    parent,
    old_path: Path,
    old_meta: UserMeta,
    new_path: Path,
    new_meta: UserMeta,
) -> bool:
    """張り替え先に既存キュレーションがあるときの併合確認。``True`` = 実行。

    ``rebind_path`` の併合分岐（★は最大 / later は OR / タグは和集合）は、
    行き先の行が独自に持っていた表明（★2 等）を黙って上書きし得る不可逆
    操作 — キュレーションはこの製品で唯一の再生成不能データなので、実行前に
    両側の内容を見せて確認する。ボタンは動詞ラベルの :func:`confirm_action`
    規約（既定 = キャンセル、Enter で誤爆しない）。Qt 層はこの関数に閉じる —
    テストはここを差し替えるだけで確認フローを通せる。
    """
    from ..common.ui import confirm_action

    return confirm_action(
        parent,
        title=t("viewer.post_grid.curation_rebind_merge_title"),
        body=t(
            "viewer.post_grid.curation_rebind_merge_body",
            old_name=old_path.name or str(old_path),
            old=_meta_summary(old_meta),
            new_name=new_path.name or str(new_path),
            new=_meta_summary(new_meta),
        ),
        accept_text=t("viewer.post_grid.curation_rebind_merge_accept"),
        destructive=True,
        # 本文にはフォルダ名・ユーザータグ（自由入力）が埋まる — AutoText の
        # HTML 解釈に乗せない（plugin_host のマニフェスト表示と同じ理由）。
        plain_text=True,
    )


def rebind_and_patch(
    store: UserMetaStore,
    meta_map: CurationMap,
    old_path: str,
    new_path: str,
) -> UserMeta | None:
    """``rebind_path`` を実行し、in-memory の地図も同じ結果へ揃える。

    ``_user_meta_map`` は描画（バッジ）と横断一覧の母集合の真実源なので、
    DB だけ直すと再起動まで一覧が旧行を数え続ける。旧綴りを外し、ストアが
    書いたのと同じ表示綴り（:func:`absolute_spelling`）で張り替え後の行を
    載せる — ``PostGrid._apply_curation`` が書き込み後に行うのと同じ規約。

    Returns 張り替え後の :class:`UserMeta`（成功時）。旧行不在・書き込み失敗は
    ``None`` で、地図には触れない（ストアと地図が別々の答えを持たない）。
    """
    meta = store.rebind_path(old_path, new_path)
    if meta is None:
        return None
    meta_map.pop(old_path, None)
    meta_map[absolute_spelling(new_path)] = meta
    return meta


__all__ = [
    "build_ghost_entries",
    "confirm_prefix_rebind",
    "confirm_rebind_merge",
    "looks_like_file",
    "plan_prefix_rebind",
    "prefix_rebind_group",
    "prompt_current_location",
    "prompt_rebind_prefix",
    "rebind_and_patch",
]
