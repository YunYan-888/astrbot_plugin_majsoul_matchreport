from dataclasses import asdict, dataclass, field
from datetime import datetime
from math import ceil
from types import SimpleNamespace
from typing import TypeVar, cast

from astrbot.api import logger

from ..lib.lq import (
    HuleInfo,
    RecordAnGangAddGang,
    RecordBaBei,
    RecordChiPengGang,
    RecordDealTile,
    RecordDiscardTile,
    RecordHule,
    RecordHuleXueZhanEnd,
    RecordHuleXueZhanMid,
    RecordLiuJu,
    RecordNewRound,
    RecordNoTile,
)
from ..model import MjsLog, MjsLogItem
from .cfg import cfg
from .constants import DAISANGEN, DAISUUSHI, JPNAME, RUNES, YSCORE
from .model import (
    Agari,
    AgariPoint,
    AnkanSymbol,
    ChiSymbol,
    DaiminkanSymbol,
    DiscardSymbol,
    KakanSymbol,
    Kyoku,
    PeSymbol,
    PonSymbol,
    Round,
    Ryukyoku,
    SingleAgari,
    SpecialRyukyoku,
    Tile,
    TileType,
    Yaku,
    ZeroSymbol,
)
from .utils import pad_list, relative_seating

T = TypeVar("T")


def _mode_room_name(mode_id) -> str:
    """mode_id → 房间名。新玩法的 mode_id 可能不在本地 cfg 表里，缺表时返回兜底名字。"""
    try:
        entry = cfg["desktop"]["matchmode"]["map_"].get(str(mode_id))
    except Exception:  # noqa: BLE001 - 本地表结构异常也不能让牌谱解析失败
        entry = None
    name = ""
    if isinstance(entry, dict):
        name = str(entry.get("room_name_jp") or entry.get("room_name") or "")
    return name or f"mode{int(mode_id or 0)}"


def _level_full_name(level_id) -> str:
    """段位 id → 日文段位名。缺表时返回空串（牌谱里那段位就是空的）。"""
    try:
        entry = cfg["level_definition"]["level_definition"]["map_"].get(str(level_id))
    except Exception:  # noqa: BLE001
        entry = None
    if isinstance(entry, dict):
        return str(entry.get("full_name_jp") or entry.get("full_name") or "")
    return ""


@dataclass
class TenhouModel:
    ver: str = "2.3"
    ref: str = ""
    ratingc: str = ""
    rule: dict = field(default_factory=dict)
    lobby: int = 0
    dan: list[str] = field(default_factory=list)
    rate: list[int] = field(default_factory=list)
    sx: list[str] = field(default_factory=list)
    name: list[str] = field(default_factory=list)
    sc: list[float] = field(default_factory=list)
    title: list[str] = field(default_factory=list)
    log: list[list[dict]] = field(default_factory=list)


class MajsoulPaipuParser:
    def __init__(self):
        self.kyokus: list[Kyoku] = []
        self.kyoku: Kyoku | None = None
        # 三麻无自摸损模式开关，默认关闭；某些牌谱不会进入设置分支
        self.tsumoloss_off: bool = False
        # 解析过程中被跳过的动作（新玩法/未知字段），供上层写进牌谱诊断
        self.parse_errors: list[str] = []
        # 血战 / 万象修罗：一局里的中途和牌（等 End 动作再落成这一局的结果）
        self._xuezhan_mid_hules: list = []
        # 每局和牌者的真实手牌：id(kyoku) -> {seat: {hand, ming, hu_tile, zimo}}
        self._agari_hands: dict = {}

    def handle_game_record(self, record: MjsLog):
        self.tsumoloss_off = False
        self.parse_errors = []
        self._xuezhan_mid_hules = []
        self._agari_hands = {}
        res = TenhouModel()
        ruledisp = ""
        lobby = ""  # usually 0, is the custom lobby number
        nplayers = len(record.head.result.players)
        nakas = nplayers - 1  # default

        # mlog version number
        res.ver = "2.3"
        # game id - copy and paste into "other" on the log page to view
        res.ref = record.head.uuid

        # PF4 is yonma, PF3 is sanma
        res.ratingc = f"PF{nplayers}"

        # rule display
        if nplayers == 3:
            ruledisp += RUNES["sanma"][JPNAME]

        if record.head.config.meta.mode_id:  # ranked or casual
            # 新玩法（万象修罗之类）的 mode_id 可能不在本地 cfg 里，缺表时不能让整局牌谱解析失败
            ruledisp += _mode_room_name(record.head.config.meta.mode_id)
        elif record.head.config.meta.room_id:  # friendly
            # can set room number as lobby number
            lobby = f": {record.head.config.meta.room_id}"
            ruledisp += RUNES["friendly"][JPNAME]  # "Friendly"
            nakas = record.head.config.mode.detail_rule.dora_count
            self.tsumoloss_off = (
                nplayers == 3 and not record.head.config.mode.detail_rule.have_zimosun
            )
        elif record.head.config.meta.contest_uid:  # tourney
            lobby = f": {record.head.config.meta.contest_uid}"
            ruledisp += RUNES["tournament"][JPNAME]  # "Tournament"
            nakas = record.head.config.mode.detail_rule.dora_count
            self.tsumoloss_off = (
                nplayers == 3 and not record.head.config.mode.detail_rule.have_zimosun
            )
        if record.head.config.mode.mode == 1:
            ruledisp += RUNES["tonpuu"][JPNAME]  # " East"
        elif record.head.config.mode.mode == 2:
            ruledisp += RUNES["hanchan"][JPNAME]

        if (
            record.head.config.meta.mode_id == 0
            and record.head.config.mode.detail_rule.dora_count == 0
        ):
            res.rule = {
                "disp": ruledisp,
                "aka53": 0,
                "aka52": 0,
                "aka51": 0,
            }
        else:
            res.rule = {
                "disp": ruledisp,
                "aka53": 1,
                "aka52": 2 if nakas == 4 else 1,
                "aka51": 1 if nplayers == 4 else 0,
            }

        # tenhou custom lobby
        # could be tourney id or friendly room for
        # mjs. appending to title instead to avoid 3->C etc. in tenhou.net/5
        res.lobby = 0

        # autism to fix logs with AI
        # ranks
        res.dan = [""] * nplayers
        for e in record.head.accounts:
            seat = int(getattr(e, "seat", 0) or 0)
            if 0 <= seat < nplayers:
                res.dan[seat] = _level_full_name(getattr(getattr(e, "level", None), "id", 0))

        # level score, no real analog to rate
        res.rate = [0] * nplayers
        for e in record.head.accounts:
            seat = int(getattr(e, "seat", 0) or 0)
            if 0 <= seat < nplayers:
                res.rate[seat] = int(getattr(getattr(e, "level", None), "score", 0) or 0)

        # sex
        res.sx = ["C"] * nplayers

        # >names
        res.name = ["AI"] * nplayers
        for e in record.head.accounts:
            seat = int(getattr(e, "seat", 0) or 0)
            if 0 <= seat < nplayers:
                res.name[seat] = str(getattr(e, "nickname", "") or "AI")

        # clean up for sanma AI
        if nplayers == 3:
            # 某些三麻记录会返回3人数组，这里补齐到4位占位，避免索引越界。
            while len(res.name) < 4:
                res.name.append("")
            while len(res.sx) < 4:
                res.sx.append("")
            res.name[3] = ""
            res.sx[3] = ""

        # scores
        scores = [
            [e.seat, e.part_point_1, e.total_point / 1000]
            for e in record.head.result.players
        ]
        res.sc = [0.0] * nplayers * 2
        for i, e in enumerate(scores):
            res.sc[2 * e[0]] = e[1]
            res.sc[2 * e[0] + 1] = e[2]

        # optional title - why not give the room and put the timestamp here
        res.title = [
            ruledisp + lobby,
            datetime.fromtimestamp(record.head.end_time).strftime("%Y-%m-%d %H:%M:%S"),
        ]

        for item in record.data:
            # 单条 action 解析失败（新玩法/未知动作）时跳过，别把整局牌谱丢掉
            try:
                self.handle(item)
            except Exception as exc:  # noqa: BLE001
                detail = f"{item.name}: {exc.__class__.__name__}: {exc}"
                if len(self.parse_errors) < 40:
                    self.parse_errors.append(detail)
                logger.warning(f"[majsoul-review] 跳过无法解析的牌谱动作 {detail}")

        res.log = [kyoku.dump() for kyoku in self.kyokus]

        # 把和牌的真实手牌挂在结算后面（和牌/放铳文本不受影响：遍历是按 delta+agari 成对读的）
        for index, kyoku in enumerate(self.kyokus):
            hands = self._agari_hands.get(id(kyoku))
            if not hands or index >= len(res.log):
                continue
            entry = res.log[index]
            if isinstance(entry, list) and entry and isinstance(entry[-1], list):
                entry[-1].append({"hais": hands})

        return asdict(res)

    def handle(self, log: MjsLogItem):
        # 有些记录源（新玩法/比赛场）直接给 ActionXxx 命名，这里统一成 RecordXxx 再分发
        name = log.name
        if name.startswith("Action"):
            name = f"Record{name[6:]}"
        match name:
            case "RecordNewRound":
                self._handle_new_round(cast(RecordNewRound, log.data))
            case "RecordDiscardTile":
                self._handle_discard_tile(cast(RecordDiscardTile, log.data))
            case "RecordDealTile":
                self._handle_deal_tile(cast(RecordDealTile, log.data))
            case "RecordChiPengGang":
                self._handle_chi_peng_gang(cast(RecordChiPengGang, log.data))
            case "RecordAnGangAddGang":
                self._handle_an_gang_add_gang(cast(RecordAnGangAddGang, log.data))
            case "RecordBaBei":
                self._handle_ba_bei(cast(RecordBaBei, log.data))
            case "RecordLiuJu":
                self._handle_liu_ju(cast(RecordLiuJu, log.data))
            case "RecordNoTile":
                self._handle_no_tile(cast(RecordNoTile, log.data))
            case "RecordHule":
                self._handle_hu_le(cast(RecordHule, log.data))
            case "RecordHuleXueZhanMid":
                self._handle_hu_le_xue_zhan_mid(cast(RecordHuleXueZhanMid, log.data))
            case "RecordHuleXueZhanEnd":
                self._handle_hu_le_xue_zhan_end(cast(RecordHuleXueZhanEnd, log.data))
            case _:
                # 万象修罗等新玩法会带本地 proto 里没有的动作，忽略即可，别报错
                logger.debug(f"[majsoul-review] 忽略未知牌谱动作: {name}")

    def _handle_new_round(self, log: RecordNewRound):
        nplayers = len(log.scores)
        self.kyoku = Kyoku(
            nplayers=nplayers,
            round=Round(4 * log.chang + log.ju, log.ben, log.liqibang),
            initscores=pad_list(list(log.scores), 4, 0),
            doras=(
                [Tile.parse(log.dora)]
                if log.dora
                else [Tile.parse(t) for t in log.doras]
            ),
            draws=[[], [], [], []],
            discards=[[], [], [], []],
            haipais=[
                [Tile.parse(t) for t in getattr(log, f"tiles{i}")]
                for i in range(nplayers)
            ],
            poppedtile=Tile(0, TileType.M),  # placeholder, will be set below
            # information we need, but can 't expect in every record
            dealerseat=log.ju,
        )

        self.kyoku.draws[log.ju].append(self.kyoku.haipais[log.ju].pop())

    def _handle_discard_tile(self, log: RecordDiscardTile):
        assert self.kyoku is not None, "discard tile before new round"
        tile = Tile.parse(log.tile)

        tsumogiri = log.moqie
        # 特判庄家第一张的手摸切
        if (
            log.seat == self.kyoku.dealerseat
            and len(self.kyoku.discards[log.seat]) == 0
            and tile == self.kyoku.poppedtile
        ):
            tsumogiri = True

        sym = DiscardSymbol(tile, tsumogiri)

        # 立直宣言
        if log.is_liqi:
            self.kyoku.priichi = True
            sym = DiscardSymbol(sym.tile, sym.tsumogiri, True)

        self.kyoku.discards[log.seat].append(sym)
        self.kyoku.ldseat = log.seat

        # 更新dora
        if len(log.doras) > len(self.kyoku.doras):
            self.kyoku.doras = [Tile.parse(t) for t in log.doras]

    def _accept_riichi(self):
        assert self.kyoku is not None, "accept riichi before new round"
        if self.kyoku.priichi:
            self.kyoku.priichi = False
            self.kyoku.nriichi += 1

    def _handle_deal_tile(self, log: RecordDealTile):
        assert self.kyoku is not None, "deal tile before new round"
        self._accept_riichi()

        # 更新dora
        if len(log.doras) > len(self.kyoku.doras):
            self.kyoku.doras = [Tile.parse(t) for t in log.doras]

        self.kyoku.draws[log.seat].append(Tile.parse(log.tile))

    def _handle_chi_peng_gang(self, log: RecordChiPengGang):
        assert self.kyoku is not None, "chi/peng/gang before new round"
        self._accept_riichi()

        if log.type == 0:
            # chii
            self.kyoku.draws[log.seat].append(
                ChiSymbol(
                    Tile.parse(log.tiles[2]),
                    Tile.parse(log.tiles[0]),
                    Tile.parse(log.tiles[1]),
                )
            )
        elif log.type == 1:
            # pon
            worktiles = [Tile.parse(t) for t in log.tiles]
            idx = relative_seating(log.seat, self.kyoku.ldseat)
            self.kyoku.countpao(worktiles[0], log.seat, self.kyoku.ldseat)
            # pop the called tile and prepend 'p'
            self.kyoku.draws[log.seat].append(
                PonSymbol(worktiles[0], worktiles[1], worktiles[2], idx)
            )
        elif log.type == 2:
            # daiminkan
            calltiles = [Tile.parse(t) for t in log.tiles]
            idx = relative_seating(log.seat, self.kyoku.ldseat)
            self.kyoku.countpao(calltiles[0], log.seat, self.kyoku.ldseat)
            self.kyoku.draws[log.seat].append(
                DaiminkanSymbol(
                    calltiles[0], calltiles[1], calltiles[2], calltiles[3], idx
                )
            )
            # tenhou drops a 0 in discards for this
            self.kyoku.discards[log.seat].append(ZeroSymbol())
            # register kan
            self.kyoku.nkan += 1
        else:
            raise RuntimeError(f"invalid RecordChiPengGang.type={log.type}")

    def _handle_an_gang_add_gang(self, log: RecordAnGangAddGang):
        assert self.kyoku is not None, "an/kan before new round"

        # NOTE: e.tiles here is a single tile; naki is placed in discards
        tile = Tile.parse(log.tiles)
        self.kyoku.ldseat = log.seat

        if log.type == 3:
            # ankan
            # mjs chun ankan example record:
            # {"seat":0,"type":3,"tiles":"7z"}

            # count the group as visible, but don't set pao
            self.kyoku.countpao(tile, log.seat, -1)

            # get the tiles from haipai and draws that
            # are involved in ankan, dumb
            # because n aka might be involved
            ankantiles = [
                t for t in self.kyoku.haipais[log.seat] if t.deaka() == tile.deaka()
            ] + [
                t
                for t in self.kyoku.draws[log.seat]
                if isinstance(t, Tile) and t.deaka() == tile.deaka()
            ]

            # doesn't really matter which tile we mark ankan with - choosing last drawn
            ankan_tile = ankantiles.pop() if ankantiles else tile

            self.kyoku.discards[log.seat].append(AnkanSymbol(ankan_tile.deaka()))
            self.kyoku.nkan += 1

        elif log.type == 2:
            # shouminkan
            # get pon naki from .draws and swap in new symbol
            for i, sy in enumerate(self.kyoku.draws[log.seat]):
                if isinstance(sy, PonSymbol) and (
                    sy.tile == tile or sy.tile == tile.deaka()
                ):
                    # remove the pon from draws and add kakan to discards
                    self.kyoku.draws[log.seat].pop(i)
                    self.kyoku.discards[log.seat].append(
                        KakanSymbol(sy.a, sy.b, sy.tile, tile, sy.feeder_relative)
                    )
                    self.kyoku.nkan += 1
                    break
        else:
            raise RuntimeError(f"invalid RecordAnGangAddGang.type={log.type}")

    def _handle_ba_bei(self, log: RecordBaBei):
        assert self.kyoku is not None, "ba bei before new round"
        # kita - this record (only) gives {seat, moqie}
        self.kyoku.discards[log.seat].append(PeSymbol())
        self.kyoku.ldseat = log.seat

    def _handle_liu_ju(self, log: RecordLiuJu):
        assert self.kyoku is not None, "liu ju before new round"
        self._accept_riichi()

        if log.type == 1:
            self.kyoku.result = SpecialRyukyoku.kyushukyuhai
        elif log.type == 2:
            self.kyoku.result = SpecialRyukyoku.sufonrenda
        elif self.kyoku.nriichi == 4:
            self.kyoku.result = SpecialRyukyoku.suuchariichi
        elif self.kyoku.nkan == 4:
            self.kyoku.result = SpecialRyukyoku.suukaikan
        else:
            raise RuntimeError(f"invalid RecordLiuJu.type={log.type}")

        self.kyokus.append(self.kyoku)
        self.kyoku = None

    def _handle_no_tile(self, log: RecordNoTile):
        assert self.kyoku is not None, "no tile before new round"
        delta = [0, 0, 0, 0]

        # NOTE: mjs wll not give delta_scores if everyone is (no)ten
        # TODO: minimize the autism
        if (
            log.scores
            and len(log.scores) > 0
            and log.scores[0].delta_scores is not None
            and len(log.scores[0].delta_scores) != 0
        ):
            for score in log.scores:
                for i, g in enumerate(score.delta_scores):
                    # for the rare case of multiple nagashi, we sum the arrays
                    delta[i] += g

        self.kyoku.result = Ryukyoku(delta, getattr(log, "liujumanguan", False))

        self.kyokus.append(self.kyoku)
        self.kyoku = None

    def _tlround(self, x: float):
        """
        round up to nearest hundred iff TSUMOLOSSOFF == true otherwise return 0
        """
        if self.tsumoloss_off:
            return 100 * ceil(x / 100)
        else:
            return 0

    def _parse_hu_le(self, hule: HuleInfo, is_head_bump: bool) -> SingleAgari:
        assert self.kyoku is not None, "deal tile before new round"

        # tenhou log viewer requires 点, 飜) or 役満) to end strings
        # rest of scoring string is entirely optional
        # let res    = [h.seat, h.zimo ? h.seat : kyoku.ldseat, h.seat];
        delta = []  # we need to compute the delta ourselves to handle double/triple ron
        points = None

        # riichi stick points
        rp = (
            1000 * (self.kyoku.nriichi + self.kyoku.round.riichi_sticks)
            if is_head_bump
            else 0
        )

        # base honba payment
        hb = 100 * self.kyoku.round.honba if is_head_bump else 0

        # sekinin barai logic
        pao = False
        liableseat = -1
        liablefor = 0

        if hule.yiman:
            # only worth checking yakuman hands
            for e in hule.fans:
                if e.id == DAISUUSHI and self.kyoku.paowind != -1:
                    pao = True
                    liableseat = self.kyoku.paowind
                    liablefor += e.val  # realistically can only be liable once
                elif e.id == DAISANGEN and self.kyoku.paodrag != -1:
                    pao = True
                    liableseat = self.kyoku.paodrag
                    liablefor += e.val

        if hule.zimo:
            # ko-oya payment for non-dealer tsumo
            # delta  = [...new Array(kyoku.nplayers)].map(()=> (-hb - h.point_zimo_xian));
            if getattr(hule, "_exact_points", False):
                tlround_part = 0
            else:
                tlround_part = self._tlround((1 / 2) * hule.point_zimo_xian)
            delta = [-hb - hule.point_zimo_xian - tlround_part] * self.kyoku.nplayers
            if hule.seat == self.kyoku.dealerseat:  # oya tsumo
                delta[hule.seat] = (
                    rp
                    + (self.kyoku.nplayers - 1) * (hb + hule.point_zimo_xian)
                    + 2 * tlround_part
                )
                points = AgariPoint(
                    tsumo=hule.point_zimo_xian + tlround_part,
                    oya=True,
                )
            else:  # ko tsumo
                delta[hule.seat] = (
                    rp
                    + hb
                    + hule.point_zimo_qin
                    + (self.kyoku.nplayers - 2) * (hb + hule.point_zimo_xian)
                    + 2 * tlround_part
                )
                delta[self.kyoku.dealerseat] = -hb - hule.point_zimo_qin - tlround_part
                points = AgariPoint(
                    tsumo=hule.point_zimo_xian, tsumo_oya=hule.point_zimo_qin
                )
        else:
            delta = [0] * self.kyoku.nplayers
            delta[hule.seat] = rp + (self.kyoku.nplayers - 1) * hb + hule.point_rong
            delta[self.kyoku.ldseat] = -(self.kyoku.nplayers - 1) * hb - hule.point_rong
            points = AgariPoint(ron=hule.point_rong, oya=hule.qinjia)

        # sekinin barai payments
        # treat pao as the liable player paying back
        # the other players - safe for multiple yakuman
        OYA = 0
        KO = 1
        RON = 2

        if pao:
            # this is how tenhou does it
            # doesn't really seem to matter to akochan or tenhou.net/5

            if hule.zimo:  # liable player needs to payback n yakuman tsumo payments
                if hule.qinjia:  # dealer tsumo
                    # should treat tsumo loss as ron
                    # luckily all yakuman values round safely for
                    # north bisection
                    tlround_part = self._tlround((1 / 2) * liablefor * YSCORE[OYA][KO])
                    delta[liableseat] -= (
                        2 * hb + liablefor * 2 * YSCORE[OYA][KO] + tlround_part
                    )
                    for i, e in enumerate(delta):
                        if (
                            liableseat != i
                            and hule.seat != i
                            and self.kyoku.nplayers >= i
                        ):
                            delta[i] += hb + liablefor * YSCORE[OYA][KO] + tlround_part
                    # dealer should get north's payment from liable
                    if self.kyoku.nplayers == 3:
                        delta[hule.seat] += (
                            0 if self.tsumoloss_off else liablefor * YSCORE[OYA][KO]
                        )
                else:  # non-dealer tsumo
                    tlround_part = self._tlround((1 / 2) * liablefor * YSCORE[KO][KO])
                    delta[liableseat] -= (
                        (self.kyoku.nplayers - 2) * hb
                        + liablefor * (YSCORE[KO][OYA] + YSCORE[KO][KO])
                        + tlround_part
                    )  # ^^same 1st, but ko
                    for i, e in enumerate(delta):
                        if (
                            liableseat != i
                            and hule.seat != i
                            and self.kyoku.nplayers >= i
                        ):
                            if self.kyoku.dealerseat == i:
                                delta[i] += (
                                    hb + liablefor * YSCORE[KO][OYA] + tlround_part
                                )  # ^^same 1st
                            else:
                                delta[i] += (
                                    hb + liablefor * YSCORE[KO][KO] + tlround_part
                                )  # ^^same 1st
            # ron
            else:
                # liable seat pays the deal-in seat 1/2 yakuman + full honba
                points_ron = liablefor * YSCORE[OYA if hule.qinjia else KO][RON]
                player_num = self.kyoku.nplayers - 1
                delta[liableseat] -= int(player_num * hb + 0.5 * points_ron)
                delta[self.kyoku.ldseat] += int(player_num * hb + 0.5 * points_ron)

        return SingleAgari(
            seat=hule.seat,
            ldseat=hule.seat if hule.zimo else self.kyoku.ldseat,
            paoseat=liableseat if pao else hule.seat,
            han=hule.count,
            fu=hule.fu,
            yaku=[Yaku(e.id, e.val) for e in hule.fans],
            oya=hule.qinjia,
            tsumo=hule.zimo,
            yakuman=hule.yiman,
            point=points,
            delta=delta,
        )

    def _handle_hu_le(self, log: RecordHule):
        assert self.kyoku is not None, "hu le before new round"
        agari = []
        ura = []
        is_head_bump = True

        for f in log.hules:
            if f.li_doras is not None and len(ura) < len(f.li_doras):
                ura = [Tile.parse(t) for t in f.li_doras]
            agari.append(self._parse_hu_le(f, is_head_bump))
            is_head_bump = False  # subsequent rons don't get points

        self._remember_agari_hands(log.hules)
        self.kyoku.result = Agari(agari=agari, uras=ura, round=self.kyoku.round)
        self.kyokus.append(self.kyoku)
        self.kyoku = None

    def _remember_agari_hands(self, hules) -> None:
        """记下这一局和牌者的真实手牌。

        万象修罗这类玩法会换牌、还带癞子，靠「配牌 + 摸打」还原出来的牌面是错的，
        所以直接把结算里给的手牌/和了牌存下来，之后拼役满图片时优先用它。
        """
        if self.kyoku is None:
            return
        hands = {}
        for hule in hules or []:
            try:
                seat = int(getattr(hule, "seat", -1))
            except (TypeError, ValueError):
                continue
            if seat < 0:
                continue
            hands[seat] = {
                "hand": [str(t) for t in (getattr(hule, "hand", None) or [])],
                "ming": [str(m) for m in (getattr(hule, "ming", None) or [])],
                "hu_tile": str(getattr(hule, "hu_tile", "") or ""),
                "zimo": bool(getattr(hule, "zimo", False)),
            }
        if hands:
            self._agari_hands[id(self.kyoku)] = hands

    def _handle_hu_le_xue_zhan_mid(self, log: RecordHuleXueZhanMid):
        """血战 / 万象修罗：一局中途有人和牌（之后可能继续打），先记下来。

        真正的结算在 `RecordHuleXueZhanEnd` 里，这里只攒数据，否则整局一个 kyoku 都落不下来。
        """
        if self.kyoku is None:
            return
        # 这一手是荣和的话，放铳的人就是这局最后一张弃牌的人（血战中途和牌只给位置，不给放铳者）
        ldseat = getattr(self.kyoku, "ldseat", None)
        try:
            ldseat = int(ldseat)
        except (TypeError, ValueError):
            ldseat = -1
        for hule in getattr(log, "hules", []) or []:
            self._xuezhan_mid_hules.append((hule, ldseat))

    @staticmethod
    def _xue_zhan_base(han: int, fu: int, yakuman: bool) -> int:
        """番/符 → 基本点（用于血战中途和牌没有点数时的估算）。"""
        if yakuman or han >= 13:
            return 8000
        if han >= 11:
            return 6000
        if han >= 8:
            return 4000
        if han >= 6:
            return 3000
        if han >= 5:
            return 2000
        try:
            base = int(fu) * (2 ** (int(han) + 2))
        except Exception:  # noqa: BLE001
            base = 0
        return min(base, 2000)

    @staticmethod
    def _xue_zhan_pay(x: int) -> int:
        """点数向上取整到百位（和雀魂实际结算一致）。"""
        if x <= 0:
            return 0
        return int(100 * ceil(x / 100))

    def _xue_zhan_mid_to_hule(self, hule) -> SimpleNamespace:
        """把 `HuInfoXueZhanMid` 补成 `_parse_hu_le` 能吃的对象（缺的点数按番/符算）。"""
        seat = int(getattr(hule, "seat", 0) or 0)
        han = int(getattr(hule, "count", 0) or 0)
        fu = int(getattr(hule, "fu", 0) or 0)
        yakuman = bool(getattr(hule, "yiman", False))
        tsumo = bool(getattr(hule, "zimo", False))
        dealer = self.kyoku is not None and seat == self.kyoku.dealerseat
        base = self._xue_zhan_base(han, fu, yakuman)
        ron = 0
        zimo_xian = 0
        zimo_qin = 0
        if tsumo:
            zimo_xian = self._xue_zhan_pay(base * 2 if dealer else base)
            zimo_qin = self._xue_zhan_pay(base * 2)
        else:
            ron = self._xue_zhan_pay(base * (6 if dealer else 4))
        return SimpleNamespace(
            seat=seat,
            zimo=tsumo,
            qinjia=dealer,
            yiman=yakuman,
            count=han,
            fu=fu,
            fans=list(getattr(hule, "fans", []) or []),
            li_doras=None,
            doras=list(getattr(hule, "doras", []) or []),
            point_rong=ron,
            point_zimo_qin=zimo_qin,
            point_zimo_xian=zimo_xian,
            _exact_points=True,  # 点数已按番/符算好，别再走 _tlround 的补偿
        )

    def _handle_hu_le_xue_zhan_end(self, log: RecordHuleXueZhanEnd):
        """血战 / 万象修罗：一局结束的结算动作。

        优先用 `hules_history`（完整 HuleInfo，带点数）；没有就退回中途攒下的
        `HuInfoXueZhanMid`，用番/符估点数——至少保证和牌信息能播出来。
        """
        if self.kyoku is None:
            return
        history = list(getattr(log, "hules_history", []) or [])
        mids = list(self._xuezhan_mid_hules or [])
        if not mids:
            mids = [(hule, -1) for hule in (getattr(log, "hules", []) or [])]
        self._xuezhan_mid_hules = []

        def mid_ldseat(idx: int) -> int:
            if idx >= len(mids):
                return -1
            item = mids[idx]
            if isinstance(item, tuple):
                return int(item[1])
            return -1

        agari = []
        ura = []
        is_head_bump = True
        if history:
            for idx, hule in enumerate(history):
                li = getattr(hule, "li_doras", None)
                if li is not None and len(ura) < len(li):
                    ura = [Tile.parse(t) for t in li]
                agari.append(self._parse_hu_le_ldseat(hule, is_head_bump, mid_ldseat(idx)))
                is_head_bump = False  # 后续荣和不重复算点棒/本场
        else:
            for idx, item in enumerate(mids):
                hule = item[0] if isinstance(item, tuple) else item
                agari.append(
                    self._parse_hu_le_ldseat(
                        self._xue_zhan_mid_to_hule(hule), is_head_bump, mid_ldseat(idx)
                    )
                )
                is_head_bump = False

        self._remember_agari_hands(
            history or [item[0] if isinstance(item, tuple) else item for item in mids]
        )
        self.kyoku.result = Agari(agari=agari, uras=ura, round=self.kyoku.round)
        self.kyokus.append(self.kyoku)
        self.kyoku = None

    def _parse_hu_le_ldseat(self, hule, is_head_bump: bool, ldseat: int):
        """带着「放铳者」解析和牌（血战的结算记录里没有放铳者，用中途和牌时记下的座位）。"""
        if self.kyoku is None or ldseat is None or int(ldseat) < 0:
            return self._parse_hu_le(hule, is_head_bump)
        old = self.kyoku.ldseat
        self.kyoku.ldseat = int(ldseat)
        try:
            return self._parse_hu_le(hule, is_head_bump)
        finally:
            self.kyoku.ldseat = old
