"""「この検出結果を信頼して実行に進めてよいか」の唯一の判定（Qt 非依存）.

同じ問い（検出済みマニフェスト + ``plugins.json`` の記録から、この id を
無確認で走らせてよいか）を持つ面は 2 つある:

* bootstrap の初回/再確認判定 :meth:`PluginHost.new_decisions`
  （起動時。信頼できなければ確認モーダルで問い直す）
* 管理ダイアログの有効化 :meth:`PluginManagerDialog._confirm_trust`
  （ユーザーがチェックを付けて OK を押した瞬間。なりすまし疑いだけ問い直す）

帰結（黙って進む vs どの文面で問い直すか）は違っても**規則は同じでなければ
ならない** — 片側にだけ規則が足される事故はこの問いを手書きで複製した
ところから出た。判定表を本モジュールの 1 関数へ寄せ、両者はその答えを
読むだけにする。

どちらの面も**ユーザーに問い直せる**（ウィンドウ構築後）ことが前提。答えを
間違えると危険な問いを、問い直せない場所へ持ち出さないこと — ウィンドウ
構築前の AI 可用性ゲート（``ai_pack.maybe_enable_from_plugins``）はこの判定を
持たず、``plugins.json`` の有効化記録を読んで UI の骨組みを立てるだけで、
プラグインのコードを走らせる決定には一切関わらない。

依存の向き: 本モジュールは host 層に属し、``store`` は**引数で受ける**
（manifest 層が store を import しない現行の向きをそのまま保つ）。Qt にも
プラグインのコードにも触れない。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ...common.i18n import t

if TYPE_CHECKING:  # 実行時 import 不要（型注釈のみ）
    from .manifest import BrokenPlugin, PluginManifest
    from .store import PluginStore

#: 信頼判定の答え。
#:
#: * ``"trusted"`` — 記録と実体が一致する（または旧レコードで候補が 1 つだけ）。
#:   無確認で走らせてよい。
#: * ``"needs_confirm"`` — 未確認 / 記録と実体の不一致 / 旧レコード × id 重複。
#:   ユーザーに問い直せる場面なら問い直し、問い直せない場面（起動ゲート）は
#:   安全側に倒して何もしない。
#: * ``"refused"`` — その id の有効なマニフェストが検出結果に無い。問い直す
#:   対象すら存在しないので、どちらの呼び出し元も「無効のまま」で終わる。
TrustVerdict = Literal["trusted", "needs_confirm", "refused"]

#: 判定の理由（ログ・テスト用。ユーザー向け文言ではない）。
TrustReason = Literal[
    "ok", "no_manifest", "unknown", "folder_mismatch", "duplicate_id"
]


@dataclass(frozen=True)
class TrustDecision:
    """:func:`trust_decision` の答え（判定 + 理由 + 勝者マニフェスト）。"""

    verdict: TrustVerdict
    reason: TrustReason
    #: 検出の勝者（``reason == "no_manifest"`` のときだけ ``None``）。信頼した
    #: 側が vendor/ の場所やフォルダ名を取るために使う。
    manifest: PluginManifest | None
    #: ``store`` が有効化確定時に記録したフォルダ basename（旧レコードは ``None``）。
    recorded: str | None
    #: 同じ id を名乗って検出に敗れたフォルダの basename（辞書順）。
    #: ``reason == "duplicate_id"`` のときだけ埋まる — ユーザーが「どのフォルダを
    #: 信じるか」を判断できる唯一の材料なので、理由と一緒に持ち歩く。
    competing: tuple[str, ...] = ()


def trust_decision(
    pid: str,
    *,
    manifests: Sequence[PluginManifest] | Iterable[PluginManifest],
    broken: Iterable[BrokenPlugin] | None,
    store: PluginStore,
) -> TrustDecision:
    """*pid* を無確認で走らせてよいかを判定する（副作用なしの純関数）。

    判定表（この 5 セルが信頼判定の全て）:

    ==========================================  ===============  ==============
    状態                                        verdict          reason
    ==========================================  ===============  ==============
    その id の有効なマニフェストが無い          refused          no_manifest
    id 重複あり（未記録 / 旧レコード）          needs_confirm    duplicate_id
    ユーザーの決定が未記録（未知 = 未確認）     needs_confirm    unknown
    記録フォルダ ≠ 検出の勝者フォルダ           needs_confirm    folder_mismatch
    上記以外（記録一致 / 単一フォルダ）         trusted          ok
    ==========================================  ===============  ==============

    「記録フォルダ ≠ 勝者」は、記録した実体が消えて別フォルダが同じ id を
    騙っている疑い（なりすまし対策）。folder
    記録の無い旧レコードは後方互換で勝者を採用するが、**同一 id が複数
    フォルダにある**ときは「どれを有効化したのか」が記録から復元できない
    ので採用しない — 判別不能な状態で辞書順の勝者を信じると、
    ユーザーが一度も見ていないフォルダのコードが無確認で走る。

    同じ理由で、**未記録 × id 重複**（詐称フォルダと正規フォルダが同時に
    置かれた新規インストール）も ``unknown`` ではなく ``duplicate_id`` に
    する。verdict はどちらも ``needs_confirm`` で変わらないが、理由が
    ``unknown`` だと :func:`confirm_prompt` が新規用の文面を選び、ユーザーが
    唯一判断できる材料（競合フォルダの名前）が本文から落ちる。

    ``store.folder`` の値は検証せずそのまま比較する: 記録が traversal 文字列
    （``../…``）でも ``plugins/`` 直下の basename とは決して一致しないため、
    ベース外のフォルダが ``trusted`` になることはない。
    """
    manifest = next((m for m in manifests if m.id == pid), None)
    if manifest is None:
        return TrustDecision("refused", "no_manifest", None, store.folder(pid))
    recorded = store.folder(pid)
    # id 重複は「未記録」より先に見る: 詐称フォルダと正規フォルダが同時に
    # 置かれる**新規インストール**が最有力の攻撃形で、そこを unknown で
    # 先に返すと、ユーザーが唯一判断できる材料（同じ id を名乗るフォルダの
    # 名前）を一切含まない新規用の文面で「有効化しますか」と聞くことになる。
    competing = tuple(
        sorted(
            b.dir.name
            for b in (broken or ())
            if getattr(b, "dup_id", None) == pid
        )
    )
    if competing and recorded is None:
        # 記録フォルダがあるなら「記録と一致するか」の方が強い材料なので、
        # 重複を理由にするのは未記録 / 旧レコードのときだけ。
        return TrustDecision(
            "needs_confirm", "duplicate_id", manifest, recorded, competing
        )
    if not store.known(pid):
        return TrustDecision("needs_confirm", "unknown", manifest, recorded)
    current = manifest.dir.name
    if recorded is not None:
        if recorded != current:
            return TrustDecision(
                "needs_confirm", "folder_mismatch", manifest, recorded
            )
        return TrustDecision("trusted", "ok", manifest, recorded)
    # ここから後方互換: folder 記録の無い旧レコード（重複が無いので勝者を採用）。
    return TrustDecision("trusted", "ok", manifest, recorded)


def confirm_prompt(decision: TrustDecision) -> tuple[str, str]:
    """再確認モーダルの ``(タイトル, 本文)`` を *decision* の理由から導く。

    「同じ判定 → 同じ問い方」を守るための単一情報源。以前は bootstrap が
    ``store.known()`` から文言を再導出しており、id 重複（``duplicate_id``）でも
    フォルダ入れ替え（``folder_mismatch``）用の本文が出ていた — ユーザーが
    唯一判断できる事実（同じ id を名乗るフォルダが複数あり、どれが勝者で
    どれが敗者か）が本文に入らないまま「有効化しますか」と聞いていた。

    本文に入るのは未検証のマニフェスト文字列なので、呼び出し側は必ず
    プレーンテキストで表示すること（``confirm_action(plain_text=True)``）。
    """
    manifest = decision.manifest
    if manifest is None:  # pragma: no cover (defensive — 呼び出し側が弾く)
        raise ValueError("confirm_prompt needs a winner manifest")
    common = {
        "name": manifest.name,
        "version": manifest.version,
        "author": manifest.author or "-",
        "description": manifest.description or "-",
    }
    if decision.reason == "duplicate_id":
        return (
            t("viewer.plugins.duplicate_prompt_title"),
            t(
                "viewer.plugins.duplicate_prompt_body",
                folder=manifest.dir.name,
                others="\n".join(decision.competing),
                **common,
            ),
        )
    if decision.reason == "folder_mismatch":
        return (
            t("viewer.plugins.changed_prompt_title"),
            t(
                "viewer.plugins.changed_prompt_body",
                folder=manifest.dir.name,
                **common,
            ),
        )
    return (
        t("viewer.plugins.new_prompt_title"),
        # 未確認の初回同意にもフォルダ名を入れる: 「どのフォルダのコードを
        # 走らせてよいか」を答える問いなのに、実体の名前が本文に無かった
        # （管理ダイアログの詳細欄・他の 2 文面には既に入っている）。
        t("viewer.plugins.new_prompt_body", folder=manifest.dir.name, **common),
    )


def declined_before(manifest: PluginManifest, store: PluginStore) -> bool:
    """*manifest* のフォルダが「確認モーダルで断られた実体」として記録済みか。

    判定表の外にある**追加の確認理由**で、読むのは後から有効化しようとする
    面（管理ダイアログ）だけ。表そのものへ入れないのは、起動時の bootstrap が
    毎回聞き直す形になり「断った実体は次回以降も無言」（``store`` の
    「なりすまし対策」節）を壊すため — 断った記録は「起動時に黙る」ためには
    十分でも、「その実体を今から走らせてよい」根拠にはならない。
    """
    return store.declined(manifest.id) and store.folder(manifest.id) == (
        manifest.dir.name
    )


def decline_prompt(manifest: PluginManifest) -> tuple[str, str]:
    """:func:`declined_before` の実体を有効化するときの ``(タイトル, 本文)``。"""
    return (
        t("viewer.plugins.declined_prompt_title"),
        t(
            "viewer.plugins.declined_prompt_body",
            name=manifest.name,
            version=manifest.version,
            author=manifest.author or "-",
            description=manifest.description or "-",
            folder=manifest.dir.name,
        ),
    )

