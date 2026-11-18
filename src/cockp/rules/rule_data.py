"""Verified numeric examples and extensible data schemas; no copyrighted prose.

Catalogs intentionally identify source PDF pages. Other creatures/weapons can be
imported through the same schema after source review; an unknown special ability
must enter manual adjudication instead of being silently treated as ordinary.
"""
from __future__ import annotations

import math
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from cockp.dice.dice import expression_bounds


class Weapon(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    skill: str
    damage: str
    damage_bonus: Literal["none", "full", "half"] = "none"
    impaling: bool = False
    range_yards: float = Field(default=0, ge=0)
    max_shots: int = Field(default=1, ge=1, le=1000)
    capacity: int = Field(default=0, ge=0, le=10000)
    malfunction: int | None = Field(default=None, ge=1, le=100)
    era: list[str] = Field(default_factory=lambda: ["1920s", "modern"])
    automatic: bool = False
    source_pdf_page: int
    special: list[str] = Field(default_factory=list)
    cadence_rounds: int = Field(default=1,ge=1,le=10)
    capacity_options: list[int] = Field(default_factory=list)
    range_damage: list[tuple[float,str]] = Field(default_factory=list)
    blast_radius: float = Field(default=0,ge=0)
    rare: bool = False

    @field_validator("damage")
    @classmethod
    def dice_expression(cls, value):
        expression_bounds(value)
        return value


def _weapon(id, name, skill, damage, page, **kwargs):
    return Weapon(id=id, name=name, skill=skill, damage=damage, source_pdf_page=page, **kwargs)


WEAPONS = {w.id: w for w in [
    _weapon("unarmed", "徒手", "格斗(斗殴)", "1D3", 86, damage_bonus="full"),
    _weapon("brass_knuckles", "黄铜指虎", "格斗(斗殴)", "1D3+1", 352, damage_bonus="full"),
    _weapon("club_large", "大棒", "格斗(斗殴)", "1D8", 352, damage_bonus="full"),
    _weapon("club_small", "警棍", "格斗(斗殴)", "1D6", 352, damage_bonus="full"),
    _weapon("knife_small", "小型刀", "格斗(斗殴)", "1D4", 352, damage_bonus="full", impaling=True),
    _weapon("knife_medium", "中型刀", "格斗(斗殴)", "1D4+2", 352, damage_bonus="full", impaling=True),
    _weapon("knife_large", "大型刀", "格斗(斗殴)", "1D8", 352, damage_bonus="full", impaling=True),
    _weapon("hatchet", "手斧", "格斗(斗殴)", "1D6+1", 352, damage_bonus="full", impaling=True),
    _weapon("axe", "伐木斧", "格斗(斧)", "1D8+2", 352, damage_bonus="full", impaling=True),
    _weapon("sword", "中型剑", "格斗(剑)", "1D6+1", 352, damage_bonus="full", impaling=True),
    _weapon("bow", "弓", "射击(弓)", "1D6", 352, damage_bonus="half", range_yards=30, capacity=1, malfunction=97),
    _weapon("crossbow", "弩", "射击(弓)", "1D8+2", 352, range_yards=50, capacity=1, malfunction=96, impaling=True, special=["fire_every_two_rounds"]),
    _weapon("rock", "投石", "投掷", "1D4", 352, damage_bonus="half", special=["range_STR_feet"]),
    _weapon("spear", "矛", "格斗(矛)", "1D8+1", 352, impaling=True),
    _weapon("garrote", "绞索", "格斗(绞索)", "1D6", 352, damage_bonus="full", impaling=True, special=["grapple_ongoing_1D6"]),
    _weapon("torch", "火把", "格斗(斗殴)", "1D6", 352, special=["burning"]),
    _weapon("chainsaw", "链锯", "格斗(链锯)", "2D8", 352, impaling=True, malfunction=95, era=["modern"], special=["double_fumble_range", "major_wound_limb_loss"]),
    _weapon("22_auto", ".22自动手枪", "射击(手枪)", "1D6", 353, impaling=True, range_yards=10, max_shots=3, capacity=6, malfunction=100),
    _weapon("32_revolver", ".32左轮", "射击(手枪)", "1D8", 353, impaling=True, range_yards=15, max_shots=3, capacity=6, malfunction=100),
    _weapon("38_revolver", ".38左轮", "射击(手枪)", "1D10", 353, impaling=True, range_yards=15, max_shots=3, capacity=6, malfunction=100),
    _weapon("38_auto", ".38自动手枪", "射击(手枪)", "1D10", 353, impaling=True, range_yards=15, max_shots=3, capacity=8, malfunction=99),
    _weapon("luger", "鲁格P08", "射击(手枪)", "1D10", 353, impaling=True, range_yards=15, max_shots=3, capacity=8, malfunction=99),
    _weapon("45_auto", ".45自动手枪", "射击(手枪)", "1D10+2", 353, impaling=True, range_yards=15, max_shots=3, capacity=7, malfunction=100),
    _weapon("glock17", "格洛克17", "射击(手枪)", "1D10", 353, impaling=True, range_yards=15, max_shots=3, capacity=17, malfunction=98, era=["modern"]),
    _weapon("rifle_30", ".30杠杆步枪", "射击(步枪/霰弹枪)", "2D6", 353, impaling=True, range_yards=50, capacity=6, malfunction=98),
    _weapon("lee_enfield", "李恩菲尔德", "射击(步枪/霰弹枪)", "2D6+4", 353, impaling=True, range_yards=110, capacity=5, malfunction=100),
    _weapon("shotgun12", "12号双管霰弹枪", "射击(步枪/霰弹枪)", "4D6", 354, range_yards=10, max_shots=2, capacity=2, malfunction=100, special=["shotgun_ranges"]),
    _weapon("ak47", "AK47", "射击(冲锋枪)", "2D6+1", 354, impaling=True, range_yards=100, max_shots=2, capacity=30, malfunction=100, automatic=True, era=["modern"]),
    _weapon("thompson", "汤普森", "射击(冲锋枪)", "1D10+2", 355, impaling=True, range_yards=20, capacity=20, malfunction=96, automatic=True, era=["1920s"]),
    _weapon("mp5", "MP5", "射击(冲锋枪)", "1D10", 355, impaling=True, range_yards=20, capacity=30, malfunction=97, automatic=True, era=["modern"]),
    _weapon("grenade", "手榴弹", "投掷", "4D10", 355, malfunction=99, special=["explosion_3_yards"]),
    _weapon("molotov", "燃烧瓶", "投掷", "2D6", 355, malfunction=95, special=["burning", "range_STR_feet"]),
]}

# Remaining appendix numerical entries. Units are yards (three feet per yard).
# Rarity and cadence are constraints for the host, never permission to own a weapon.
for _item in [
    _weapon("whip","长鞭","格斗(鞭)","1D3",352,damage_bonus="half",range_yards=10/3,era=["1920s"]),
    _weapon("blackjack","包皮短棒","格斗(斗殴)","1D8",352,damage_bonus="full"),
    _weapon("live_wire","通电导线","格斗(斗殴)","2D8",352,malfunction=95,era=["modern"],special=["stun"]),
    _weapon("teargas","催泪瓦斯","格斗(斗殴)","0",352,range_yards=2,capacity=25,special=["teargas"]),
    _weapon("nunchaku","双节棍","格斗(连枷)","1D8",352,damage_bonus="full"),
    _weapon("shuriken","手里剑","投掷","1D3",352,damage_bonus="half",impaling=True,range_yards=20,max_shots=2,capacity=1,malfunction=100),
    _weapon("javelin","投矛","投掷","1D8",352,damage_bonus="half",impaling=True,rare=True,special=["range_STR_feet"]),
    _weapon("sword_large","大型剑","格斗(剑)","1D8+1",352,damage_bonus="full",impaling=True),
    _weapon("sword_light","轻型剑","格斗(剑)","1D6",352,damage_bonus="full",impaling=True),
    _weapon("taser_contact","接触电击枪","格斗(斗殴)","1D3",352,malfunction=97,era=["modern"],special=["taser"]),
    _weapon("taser_ranged","射击电击枪","射击(手枪)","1D3",352,range_yards=5,capacity=3,malfunction=95,era=["modern"],special=["taser"]),
    _weapon("boomerang","战斗回力镖","投掷","1D8",352,damage_bonus="half",range_yards=20,rare=True),
    _weapon("flintlock_pistol","燧发手枪","射击(手枪)","1D6+1",353,impaling=True,range_yards=10,capacity=1,malfunction=95,cadence_rounds=4,rare=True),
    _weapon("25_short",".25短口手枪","射击(手枪)","1D6",353,impaling=True,range_yards=3,capacity=1,malfunction=100,era=["1920s"]),
    _weapon("32_auto",".32自动手枪","射击(手枪)","1D8",353,impaling=True,range_yards=15,max_shots=3,capacity=8,malfunction=99),
    _weapon("357_magnum",".357马格南左轮","射击(手枪)","1D8+1D4",353,impaling=True,range_yards=15,max_shots=3,capacity=6,malfunction=100,era=["modern"]),
    _weapon("beretta_m9","贝瑞塔M9","射击(手枪)","1D10",353,impaling=True,range_yards=15,max_shots=3,capacity=15,malfunction=98,era=["modern"]),
    _weapon("41_revolver",".41左轮","射击(手枪)","1D10",353,impaling=True,range_yards=15,max_shots=3,capacity=8,malfunction=100,era=["1920s"],rare=True),
    _weapon("44_magnum",".44马格南左轮","射击(手枪)","1D10+1D4+2",353,impaling=True,range_yards=15,max_shots=3,capacity=6,malfunction=100,era=["modern"]),
    _weapon("45_revolver",".45左轮","射击(手枪)","1D10+2",353,impaling=True,range_yards=15,max_shots=3,capacity=6,malfunction=100),
    _weapon("desert_eagle","沙漠之鹰","射击(手枪)","1D10+1D6+3",353,impaling=True,range_yards=15,max_shots=3,capacity=7,malfunction=94,era=["modern"]),
    _weapon("springfield1855","1855春田步枪","射击(步枪/霰弹枪)","1D10+4",353,impaling=True,range_yards=60,capacity=1,malfunction=95,cadence_rounds=4,rare=True),
    _weapon("rifle_22",".22栓式步枪","射击(步枪/霰弹枪)","1D6+1",353,impaling=True,range_yards=30,capacity=6,malfunction=99),
    _weapon("martini_henry","马蒂尼亨利步枪","射击(步枪/霰弹枪)","1D8+1D6+3",353,impaling=True,range_yards=80,capacity=1,malfunction=100,cadence_rounds=3,era=["1920s"]),
    _weapon("moran_air_rifle","莫兰气动步枪","射击(步枪/霰弹枪)","2D6+1",353,impaling=True,range_yards=20,capacity=1,malfunction=88,cadence_rounds=3,era=["1920s"]),
    _weapon("garand","加兰德步枪","射击(步枪/霰弹枪)","2D6+4",353,impaling=True,range_yards=110,capacity=8,malfunction=100,era=["modern"]),
    _weapon("sks","SKS半自动步枪","射击(步枪/霰弹枪)","2D6+1",353,impaling=True,range_yards=90,max_shots=2,capacity=10,malfunction=97,era=["modern"]),
    _weapon("rifle_3006_bolt",".30-06手动步枪","射击(步枪/霰弹枪)","2D6+4",353,impaling=True,range_yards=110,capacity=5,malfunction=100),
    _weapon("rifle_3006_auto",".30-06半自动步枪","射击(步枪/霰弹枪)","2D6+4",353,impaling=True,range_yards=110,capacity=5,malfunction=100,era=["modern"]),
    _weapon("marlin444",".444马林步枪","射击(步枪/霰弹枪)","2D8+4",354,impaling=True,range_yards=110,capacity=5,malfunction=98,era=["modern"]),
    _weapon("elephant_rifle","双管猎象枪","射击(步枪/霰弹枪)","3D6+4",354,impaling=True,range_yards=100,max_shots=2,capacity=2,malfunction=100),
    _weapon("shotgun20","20号双管霰弹枪","射击(步枪/霰弹枪)","2D6",354,range_yards=10,max_shots=2,capacity=2,malfunction=100,era=["1920s"],range_damage=[(10,"2D6"),(20,"1D6"),(50,"1D3")]),
    _weapon("shotgun16","16号双管霰弹枪","射击(步枪/霰弹枪)","2D6+2",354,range_yards=10,max_shots=2,capacity=2,malfunction=100,era=["1920s"],range_damage=[(10,"2D6+2"),(20,"1D6+1"),(50,"1D4")]),
    _weapon("shotgun12_pump","12号泵动霰弹枪","射击(步枪/霰弹枪)","4D6",354,range_yards=10,capacity=5,malfunction=100,era=["modern"],range_damage=[(10,"4D6"),(20,"2D6"),(50,"1D6")]),
    _weapon("shotgun12_auto","12号半自动霰弹枪","射击(步枪/霰弹枪)","4D6",354,range_yards=10,max_shots=2,capacity=5,malfunction=100,era=["modern"],range_damage=[(10,"4D6"),(20,"2D6"),(50,"1D6")]),
    _weapon("shotgun12_sawn","12号锯短霰弹枪","射击(步枪/霰弹枪)","4D6",354,range_yards=5,max_shots=2,capacity=2,malfunction=100,era=["1920s"],range_damage=[(5,"4D6"),(10,"1D6")]),
    _weapon("shotgun10","10号双管霰弹枪","射击(步枪/霰弹枪)","4D6+2",354,range_yards=10,max_shots=2,capacity=2,malfunction=100,era=["1920s"],rare=True,range_damage=[(10,"4D6+2"),(20,"2D6+1"),(50,"1D4")]),
    _weapon("benelli_m3","贝里尼M3","射击(步枪/霰弹枪)","4D6",354,range_yards=10,max_shots=2,capacity=7,malfunction=100,era=["modern"],range_damage=[(10,"4D6"),(20,"2D6"),(50,"1D6")]),
    _weapon("spas12","SPAS霰弹枪","射击(步枪/霰弹枪)","4D6",354,range_yards=10,capacity=8,malfunction=98,era=["modern"],range_damage=[(10,"4D6"),(20,"2D6"),(50,"1D6")]),
    _weapon("ak74","AK74","射击(冲锋枪)","2D6+1",354,impaling=True,range_yards=110,max_shots=2,capacity=30,malfunction=97,automatic=True,era=["modern"]),
    _weapon("barrett_m82","巴雷特M82","射击(步枪/霰弹枪)","2D10+1D8+6",354,impaling=True,range_yards=250,capacity=11,malfunction=96,era=["modern"]),
    _weapon("fn_fal","FN FAL","射击(冲锋枪)","2D6+4",354,impaling=True,range_yards=110,max_shots=2,capacity=20,malfunction=97,automatic=True,era=["modern"],special=["burst_three_only"]),
    _weapon("galil","加利尔","射击(冲锋枪)","2D6",354,impaling=True,range_yards=110,max_shots=2,capacity=20,malfunction=98,automatic=True,era=["modern"]),
    _weapon("m16a2","M16A2","射击(冲锋枪)","2D6",354,impaling=True,range_yards=110,max_shots=2,capacity=30,malfunction=97,automatic=True,era=["modern"],special=["burst_three_only"]),
    _weapon("m4","M4","射击(冲锋枪)","2D6",354,impaling=True,range_yards=90,capacity=30,malfunction=97,automatic=True,era=["modern"],special=["burst_three_only"]),
    _weapon("steyr_aug","斯泰尔AUG","射击(冲锋枪)","2D6",354,impaling=True,range_yards=110,max_shots=2,capacity=30,malfunction=99,automatic=True,era=["modern"]),
    _weapon("beretta7090","贝雷塔M70/90","射击(冲锋枪)","2D6",354,impaling=True,range_yards=110,capacity=30,malfunction=99,automatic=True,era=["modern"]),
    _weapon("mp18","MP18/MP28","射击(冲锋枪)","1D10",355,impaling=True,range_yards=20,max_shots=2,capacity=20,capacity_options=[20,30,32],malfunction=96,automatic=True,era=["1920s"]),
    _weapon("mac11","MAC11","射击(冲锋枪)","1D10",355,impaling=True,range_yards=15,max_shots=3,capacity=32,malfunction=96,automatic=True,era=["modern"]),
    _weapon("skorpion","蝎式","射击(冲锋枪)","1D8",355,impaling=True,range_yards=15,max_shots=3,capacity=20,malfunction=96,automatic=True,era=["modern"]),
    _weapon("uzi","乌兹","射击(冲锋枪)","1D10",355,impaling=True,range_yards=20,max_shots=2,capacity=32,malfunction=98,automatic=True,era=["modern"]),
    _weapon("gatling1882","1882加特林","射击(机枪)","2D6+4",355,impaling=True,range_yards=100,capacity=200,malfunction=96,automatic=True,era=["1920s"],rare=True),
    _weapon("bar1918","M1918勃朗宁自动步枪","射击(机枪)","2D6+4",355,impaling=True,range_yards=90,max_shots=2,capacity=20,malfunction=100,automatic=True,era=["1920s"]),
    _weapon("browning1917","M1917A1勃朗宁机枪","射击(机枪)","2D6+4",355,impaling=True,range_yards=150,capacity=250,malfunction=96,automatic=True,era=["1920s"]),
    _weapon("bren","布伦轻机枪","射击(机枪)","2D6+4",355,impaling=True,range_yards=110,capacity=30,capacity_options=[30,100],malfunction=96,automatic=True,era=["1920s"]),
    _weapon("lewis","路易斯机枪","射击(机枪)","2D6+4",355,impaling=True,range_yards=110,capacity=27,capacity_options=[27,97],malfunction=96,automatic=True,era=["1920s"]),
    _weapon("m134","M134速射机枪","射击(机枪)","2D6+4",355,impaling=True,range_yards=200,capacity=4000,malfunction=98,automatic=True,era=["modern"],special=["unmounted_build_2"]),
    _weapon("minimi","FN米尼米","射击(机枪)","2D6",355,impaling=True,range_yards=110,capacity=30,capacity_options=[30,200],malfunction=99,automatic=True,era=["modern"]),
    _weapon("vickers","维克斯.303机枪","射击(机枪)","2D6+4",355,impaling=True,range_yards=110,capacity=250,malfunction=99,automatic=True,era=["1920s"]),
    _weapon("flare_pistol","信号枪","射击(手枪)","1D10+1D3",355,impaling=True,range_yards=10,capacity=1,malfunction=100,cadence_rounds=2,special=["burning"]),
    _weapon("m79","M79榴弹发射器","射击(重武器)","3D10",355,impaling=True,range_yards=20,capacity=1,malfunction=99,cadence_rounds=3,blast_radius=2,era=["modern"],special=["explosion"]),
    _weapon("dynamite","炸药棒","投掷","4D10",355,impaling=True,capacity=1,malfunction=99,cadence_rounds=2,blast_radius=3,special=["explosion","range_STR_feet"]),
    _weapon("detonator","雷管","电气维修","2D10",355,impaling=True,capacity=1,malfunction=100,blast_radius=1,special=["explosion"]),
    _weapon("demolition_tube","爆破筒","爆破","1D10",355,impaling=True,capacity=1,malfunction=95,blast_radius=3,special=["explosion"]),
    _weapon("c4","塑胶炸弹","爆破","6D10",355,impaling=True,capacity=1,malfunction=99,blast_radius=3,era=["modern"],special=["explosion"]),
    _weapon("mortar81","81mm迫击炮","炮术","6D10",356,impaling=True,range_yards=500,capacity=1,malfunction=100,blast_radius=6,era=["modern"],special=["explosion"]),
    _weapon("fieldgun75","75mm野战炮","炮术","10D10",356,impaling=True,range_yards=500,capacity=1,malfunction=99,cadence_rounds=4,blast_radius=2,special=["explosion"]),
    _weapon("tankgun120","120mm坦克炮","炮术","10D10",356,impaling=True,range_yards=2000,capacity=1,malfunction=100,blast_radius=2,era=["modern"],special=["explosion"]),
    _weapon("navalgun127","127mm舰炮","炮术","15D10",356,impaling=True,range_yards=3000,capacity=1,malfunction=98,blast_radius=4,special=["explosion"]),
    _weapon("antipersonnel_mine","反步兵地雷","爆破","4D10",356,impaling=True,capacity=1,malfunction=99,blast_radius=5,era=["modern"],special=["explosion"]),
    _weapon("claymore","阔剑地雷","爆破","6D6",356,impaling=True,capacity=1,malfunction=99,blast_radius=20,era=["modern"],special=["directional_explosion"]),
    _weapon("flamethrower","火焰喷射器","射击(喷射器)","2D6",356,impaling=True,range_yards=25,capacity=10,malfunction=93,special=["burning"]),
    _weapon("m72","M72轻型反坦克炮","射击(重武器)","8D10",356,impaling=True,range_yards=150,capacity=1,malfunction=98,blast_radius=1,era=["modern"],special=["explosion"]),
]:WEAPONS[_item.id]=_item
WEAPONS["crossbow"].cadence_rounds=2
WEAPONS["molotov"].cadence_rounds=2
WEAPONS["grenade"].cadence_rounds=2
WEAPONS["grenade"].blast_radius=3
WEAPONS["thompson"].capacity_options=[20,30,50]
WEAPONS["mp5"].capacity_options=[15,30]
WEAPONS["mp5"].capacity=15
WEAPONS["mp5"].max_shots=2
WEAPONS["shotgun12"].range_damage=[(10,"4D6"),(20,"2D6"),(50,"1D6")]

ARMOR = {"厚重皮夹克": 1, "一战钢盔": 2, "1英寸硬木": 3, "现代美军头盔": 5,
         "重型凯芙拉防弹背心": 8, "1.5英寸防弹玻璃": 15, "1英寸钢板": 19, "大号沙包": 20}
ARMOR_SOURCE = "PDF91 / 中文83"


class Monster(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    attributes: dict[str, int]
    hp: int
    mp: int
    mov: int
    build: int
    damage_bonus: str
    armor: int = 0
    fight: int
    dodge: int
    attacks_per_round: int = 1
    damage: str
    sanity_loss: str
    skills: dict[str, int] = Field(default_factory=dict)
    abilities: list[str] = Field(default_factory=list)
    source_pdf_pages: list[int]
    alternate_mov: dict[str,int] = Field(default_factory=dict)
    alternate_attacks: list[dict] = Field(default_factory=list)
    source_ambiguities: list[str] = Field(default_factory=list)


MONSTERS = {
    "deep_one": Monster(id="deep_one", name="深潜者", attributes={"STR": 70, "CON": 50, "SIZ": 80, "DEX": 50, "INT": 65, "POW": 50}, hp=13, mp=10, mov=8, build=1, damage_bonus="1D4", armor=1, fight=45, dodge=25, damage="1D6", sanity_loss="0/1D6", abilities=["amphibious", "swim_mov_10"], source_pdf_pages=[259]),
    "ghoul": Monster(id="ghoul", name="食尸鬼", attributes={"STR": 80, "CON": 65, "SIZ": 65, "DEX": 65, "INT": 65, "POW": 65}, hp=13, mp=13, mov=9, build=1, damage_bonus="1D4", fight=40, dodge=40, attacks_per_round=3, damage="1D6", sanity_loss="0/1D6", skills={"攀爬": 85, "潜行": 70, "跳跃": 75, "聆听": 70, "侦查": 50}, abilities=["half_firearm_impaling_damage", "bite_hold_1D4_STR_escape"], source_pdf_pages=[266, 267]),
    "ghast": Monster(id="ghast", name="妖鬼", attributes={"STR": 110, "CON": 70, "SIZ": 130, "DEX": 65, "INT": 15, "POW": 50}, hp=20, mp=10, mov=10, build=3, damage_bonus="2D6", fight=45, dodge=35, attacks_per_round=2, damage="1D6", sanity_loss="0/1D8", skills={"潜行": 70}, abilities=["sunlight_harm_requires_adjudication"], source_pdf_pages=[266]),
}
for _monster in [
    Monster(id="byakhee",name="拜亚基",attributes={"STR":90,"CON":50,"SIZ":90,"DEX":67,"INT":50,"POW":50},hp=14,mp=10,mov=5,build=2,damage_bonus="1D6",armor=2,fight=55,dodge=33,attacks_per_round=2,damage="1D6",sanity_loss="1/1D6",skills={"聆听":50,"侦查":50},abilities=["blood_drain_3D10_STR"],alternate_mov={"fly":16},alternate_attacks=[{"id":"bite_hold","kind":"maneuver","damage":"1D6","ongoing_STR_loss":"3D10","recovery_per_day":"1D10+5","escape":"opposed_STR"}],source_pdf_pages=[253],source_ambiguities=["紧咬持续中拜亚基STR如何调整，译者已表示不确定；此部分需人工裁定"]),
    Monster(id="mi_go",name="米戈",attributes={"STR":50,"CON":50,"SIZ":50,"DEX":70,"INT":65,"POW":65},hp=10,mp=13,mov=7,build=0,damage_bonus="0",fight=45,dodge=35,attacks_per_round=2,damage="1D6",sanity_loss="0/1D6",abilities=["minimum_impaling_damage","hypnosis_opposed_POW","void_light_1_MP_per_cubic_yard"],alternate_mov={"fly":13},alternate_attacks=[{"id":"lift","kind":"maneuver","maximum_target_build":0}],source_pdf_pages=[274]),
    Monster(id="nightgaunt",name="夜魇",attributes={"STR":50,"CON":50,"SIZ":70,"DEX":65,"INT":20,"POW":50},hp=12,mp=10,mov=6,build=0,damage_bonus="0",armor=2,fight=45,dodge=35,damage="1D4",sanity_loss="0/1D6",skills={"潜行":90},abilities=["tickle_requires_adjudication"],alternate_mov={"fly":12},source_pdf_pages=[275],source_ambiguities=["搔痒正文为一惩罚骰1D4轮，攻击摘要为不能移动1D6+1轮；需要一致裁定"]),
    Monster(id="rat_thing",name="人面鼠",attributes={"STR":10,"CON":35,"SIZ":5,"DEX":90,"INT":50,"POW":35},hp=4,mp=7,mov=9,build=-2,damage_bonus="-2",fight=35,dodge=45,damage="1D4",sanity_loss="0/1D6",skills={"潜行":75,"聆听":50},abilities=["running_attack_penalty_die","remove_bite_1D3"],source_pdf_pages=[275,276]),
    Monster(id="sand_dweller",name="潜沙怪",attributes={"STR":50,"CON":65,"SIZ":80,"DEX":65,"INT":50,"POW":50},hp=14,mp=10,mov=8,build=1,damage_bonus="1D4",armor=3,fight=30,dodge=30,attacks_per_round=2,damage="1D6",sanity_loss="0/1D6",skills={"潜行":55,"聆听":60,"侦查":50},source_pdf_pages=[276]),
    Monster(id="serpent_person",name="元祖蛇人",attributes={"STR":50,"CON":50,"SIZ":50,"DEX":65,"INT":80,"POW":65},hp=10,mp=13,mov=8,build=0,damage_bonus="0",armor=1,fight=50,dodge=32,damage="1D3",sanity_loss="0/1D6",skills={"恐吓":60,"科学(生物学)":40,"科学(化学)":40,"侦查":35},abilities=["bite_poison_extreme_CON_1D8"],alternate_attacks=[{"id":"bite","skill":35,"damage":"1D8","poison_damage":"1D8","resist":"extreme_CON"}],source_pdf_pages=[277]),
    Monster(id="shoggoth",name="修格斯",attributes={"STR":315,"CON":210,"SIZ":420,"DEX":15,"INT":35,"POW":50},hp=63,mp=10,mov=10,build=9,damage_bonus="8D6",fight=70,dodge=8,attacks_per_round=2,damage="0",sanity_loss="1D6/1D20",abilities=["physical_damage_one","half_fire_electricity","regenerate_2","engulf_size_pool"],source_pdf_pages=[280,281]),
    Monster(id="star_vampire",name="星之精",attributes={"STR":130,"CON":65,"SIZ":130,"DEX":40,"INT":50,"POW":75},hp=19,mp=15,mov=6,build=3,damage_bonus="2D6",armor=4,fight=45,dodge=23,attacks_per_round=3,damage="1D6",sanity_loss="1/1D10",abilities=["half_firearm_damage","blood_drain_3D10_STR","invisible_until_fed"],alternate_mov={"fly":9},source_pdf_pages=[282,283],source_ambiguities=["STR骰式2D6×50与平均130不符；采用印刷平均值，不自动按错误骰式生成"]),
]:MONSTERS[_monster.id]=_monster


def monster_damage(monster: Monster, rolled_damage: int, *, firearm: bool = False, impaling: bool = False,
                   minimum_damage: int | None = None, damage_type: str = "physical") -> dict:
    if rolled_damage < 0:
        raise ValueError("伤害不可为负")
    result = rolled_damage
    if damage_type == "enchanted": damage_type = "magic"
    if damage_type not in {"physical","fire","electricity","magic","chemical"}:
        raise ValueError("伤害类型未登记")
    if "minimum_impaling_damage" in monster.abilities and (firearm or impaling):
        if minimum_damage is None or not 0 <= minimum_damage <= rolled_damage:
            raise ValueError("米戈贯穿抗性需要武器伤害表达式的最小伤害，不能猜测")
        result=minimum_damage
    if "half_firearm_impaling_damage" in monster.abilities and (firearm or impaling):
        result //= 2
    if "half_firearm_damage" in monster.abilities and firearm:
        result //= 2
    if "physical_damage_one" in monster.abilities and damage_type=="physical":
        result=min(result,1)
    if "half_fire_electricity" in monster.abilities and damage_type in {"fire","electricity"}:
        result//=2
    return {"damage": max(0, result - monster.armor), "armor_applied": monster.armor}


def monster_regeneration(monster: Monster, hp: int) -> int:
    if not 0 <= hp <= monster.hp: raise ValueError("怪物HP非法")
    return min(monster.hp,hp+2) if "regenerate_2" in monster.abilities else hp


def weapon_profile(weapon: Weapon, *, distance: float = 0, slug: bool = False,
                   round_number: int = 1, last_fired_round: int | None = None) -> dict:
    """Source-checked range and cadence facts; caller commits last_fired_round."""
    if distance < 0 or round_number<1: raise ValueError("距离/回合非法")
    if last_fired_round is not None and round_number-last_fired_round<weapon.cadence_rounds:
        raise ValueError(f"该武器每{weapon.cadence_rounds}轮才能攻击一次")
    damage=weapon.damage;impaling=weapon.impaling;base_range=weapon.range_yards
    if slug:
        if not weapon.range_damage: raise ValueError("仅霰弹枪可选择实心铅弹")
        gauge=20 if weapon.id=="shotgun20" else 16 if weapon.id=="shotgun16" else 10 if weapon.id=="shotgun10" else 12
        damage={20:"1D10+4",16:"1D10+5",12:"1D10+6",10:"1D10+7"}[gauge]
        impaling=True;base_range=50
    elif weapon.range_damage:
        damage=next((expression for maximum,expression in weapon.range_damage if distance<=maximum),"0")
    return {"damage":damage,"impaling":impaling,"base_range":base_range,"cadence_rounds":weapon.cadence_rounds,
            "source_pdf_page":weapon.source_pdf_page}


# Numeric skill base table with a citation for each record. Dynamic attributes
# are resolved by the same pure function used during character-card review.
from cockp.documents.characters import BASE_SKILLS,base_skill
SKILLS={name:{"name":name,"base":base,"source_pdf_page":48 if name=="攀爬" else 44,
              "modern_only":name in {"计算机使用","电子学"},
              "ambiguity":"技能列表15与具体条目20矛盾，采用具体条目" if name=="攀爬" else ""}
        for name,base in BASE_SKILLS.items()}
SKILLS["闪避"]={"name":"闪避","base_formula":"DEX//2","source_pdf_page":44,"modern_only":False}
SKILLS["母语"]={"name":"母语","base_formula":"EDU","source_pdf_page":44,"modern_only":False}


def _spell(id,name,mp,san,pow,minutes,page,effect,components=None):
    return {"id":id,"name":name,"mp_cost":mp,"san_cost":san,"pow_cost":pow,"cast_minutes":minutes,
            "components":components or {},"effect":effect,"source":f"COC7th v1.2.1 PDF{page} / 中文{page-8}"}


# Costs and explicit effects are data, not permission to learn or cast. The
# engine requires registered provenance, access, study, resources, and targets.
SPELLS={spell["id"]:spell for spell in [
    _spell("bless_blade","刀锋祝福术",0,"1D4",5,60,219,{"kind":"manual","operation":"enchant_blade","minimum_sacrifice_siz":50}),
    _spell("breath_of_deep","深渊之息",8,"1D6",0,0,219,{"kind":"manual","operation":"drowning","pow_opposed":True,"cast_rounds":1}),
    _spell("blindness","致盲术",8,"2D6",0,1440,222,{"kind":"condition","condition":"blinded_permanent","pow_opposed":True,"willing_target_automatic":True}),
    _spell("cure_blindness","治盲术",8,"0",0,1440,222,{"kind":"manual","operation":"remove_blindness","pow_opposed":True,"willing_target_automatic":True,"requires_intact_eyes":True}),
    _spell("chant_thoth","透特之咏",10,"1D4",0,30,222,{"kind":"condition","condition":"thoth_mental_task_bonus","bonus_dice":1,"scope":"single_mental_task"}),
    _spell("rlyeh_mist","拉莱耶造雾术",2,"0",0,0,226,{"kind":"condition","condition":"mist_obscures_vision","duration_rounds":"1D6+4","dimensions_feet":[10,10,15]}),
    _spell("create_zombie","僵尸制造术",16,"1D6",0,10080,226,{"kind":"manual","operation":"create_zombie","final_ritual_minutes":30,"components_require_adjudication":True,"automatic_cast":True}),
    _spell("curse_rotting_skin","腐烂外皮之诅咒",5,"10",0,60,226,{"kind":"manual","operation":"hallucination","opposed_attribute":"INT","victim_san_loss":"1D10","duration_minutes":20,"night_target_penalty":1}),
    _spell("death_spell","致死术",24,"3D10",0,0,226,{"kind":"manual","operation":"progressive_internal_fire","pow_opposed_each_round":True,"cast_rounds":"1D3","range_yards":10,"damage_progression":["1D3","1D6","1D10"]}),
    _spell("dominate","支配术",1,"1",0,0,227,{"kind":"condition","condition":"dominated_until_next_round_end","pow_opposed":True,"range_yards":10,"commands_require_adjudication":True}),
    _spell("dread_curse_azathoth","阿撒托斯的恐怖诅咒",4,"1D6",0,0,227,{"kind":"manual","operation":"attribute_loss","attribute":"POW","expression":"3D6","pow_opposed":True,"cast_rounds":1}),
    _spell("dust_suleiman","苏莱曼之尘",10,"0",0,0,227,{"kind":"manual","operation":"create_powder","doses":3,"use_damage":"1D20","eligible_target":"extraterrestrial","use_rounds":1,"components_require_adjudication":True}),
    _spell("elder_sign","旧印开光术",0,"0",10,60,227,{"kind":"manual","operation":"seal_gateway","personal_armor":False}),
    _spell("enchant_sacrificial_dagger","祭刀附魔术",0,"0",30,10080,229,{"kind":"manual","operation":"enchant_sacrifice_dagger","minimum_sacrifice_pow":100}),
    _spell("mesmerize","迷身术",2,"1D6",0,0,229,{"kind":"condition","condition":"mesmerized_until_shock","pow_opposed":True,"cast_rounds":1,"requires_calm_conversation":True}),
    _spell("evil_eye","邪眼术",10,"1D4",0,0,229,{"kind":"manual","operation":"evil_eye","cast_rounds":1,"luck_multiplier":0.5,"attribute_penalty_dice":1,"firearm_malfunction_above":75,"summoning_multiplier":0.5,"luck_spending_allowed":False,"luck_growth_allowed":False}),
    _spell("green_decay","绿腐术",15,"2D8",10,1440,232,{"kind":"manual","operation":"delayed_curse","pow_opposed":True,"leaf_delivery_deadline_hours":24,"immobile_day":6,"terminal_day":7}),
    _spell("implant_fear","恐惧注入术",12,"1D6",0,0,232,{"kind":"manual","operation":"fear","victim_sanity":"0/1D6","interrupt_concentration":True}),
    _spell("mental_blast","精神震爆术",10,"1D3",0,0,233,{"kind":"manual","operation":"temporary_insanity","victim_san_loss":5,"pow_opposed":True}),
    _spell("mind_transfer","精神转移术",10,"1D10",0,0,233,{"kind":"manual","operation":"permanent_mind_exchange","pow_opposed":True,"cast_rounds_minimum":5}),
    _spell("mirror_tarkhun","塔昆阿提普之镜",5,"1",0,720,233,{"kind":"manual","operation":"mirror_projection","concentration_required":True}),
    _spell("powder_ibn_ghazi","伊本加兹之粉",1,"0",0,2880,234,{"kind":"manual","operation":"reveal_invisible_powder","doses":1,"use_rounds":1,"visible_heartbeats":10,"components_require_adjudication":True}),
    _spell("pnakotic_ankh","蒲林的埃及十字架",0,"1D6",25,0,234,{"kind":"manual","operation":"create_banishment_ankh","creation_days_formula":"20-INT//5"}),
    _spell("red_sign","修德梅尔之赤印",3,"1D8",0,0,234,{"kind":"manual","operation":"red_sign","hp_cost_each_round":1,"mp_cost_each_round":3,"cast_rounds":1,"damage_within_10_yards":"1D3","damage_10_to_30_yards":"1"}),
    _spell("resurrection","复活术",3,"1D10",0,0,235,{"kind":"manual","operation":"resurrection","cast_rounds_options":[1,2],"revived_san_loss":"1D20","complete_remains_required":True}),
    _spell("independent_binding","独立束缚术",0,"1",0,0,237,{"kind":"manual","operation":"binding","cast_rounds":1,"pow_opposed":True}),
    _spell("voorish_sign","维瑞之印",1,"1",0,0,238,{"kind":"manual","operation":"spell_assistance","cast_rounds":1,"benefit_requires_adjudication":True}),
    _spell("wave_oblivion","忘却之波",30,"1D8",0,60,238,{"kind":"manual","operation":"wave","salt_water_required":True}),
    _spell("wither_limb","肢体凋萎术",8,"1D6",0,0,238,{"kind":"manual","operation":"wither_limb","pow_opposed":True,"range_yards":10,"damage":"1D8","permanent_CON_loss":"2D10","victim_sanity":"1D4/1D8"}),
    _spell("wrack","折磨术",3,"1",0,0,238,{"kind":"condition","condition":"wracked","pow_opposed":True,"cast_rounds":1,"range_yards":10,"duration_rounds":"1D6","full_recovery_minutes":"3D10","victim_sanity":"1/1D6+1"}),
    _spell("naach_tith_barrier","纳克-提特障壁创建术",0,"1D10",0,1,225,{"kind":"manual","operation":"naach_tith_barrier","radius_yards":100,"strength_per_mp":"3D10","duration_hours":"1D4+4","prerequisites_require_adjudication":True}),
    _spell("song_hastur","哈斯塔之歌",0,"0",0,0,235,{"kind":"manual","operation":"song_hastur","cast_rounds":3,"round_mp":"1D4","round_san":"1D4","prerequisites_require_adjudication":True}),
    _spell("clutch_nyogtha","尼约格萨紧握术",1,"1D20",0,0,222,{"kind":"manual","operation":"clutch_nyogtha","pow_opposed":True,"prerequisites_require_adjudication":True}),
    _spell("melt_flesh","血肉熔解术",0,"0",0,0,232,{"kind":"manual","operation":"melt_flesh","cast_rounds":5,"prerequisites_require_adjudication":True}),
]}

VARIABLE_SPELLS={
    "cloud_memory":{"name":"记忆模糊术","mp_cost":"1D6","san_cost":"1D2","pow_cost":0,"source_pdf_page":222,"effect":"requires_opposed_POW_and_specific_event"},
    "flesh_ward":{"name":"血肉防护术","mp_cost":"chosen","san_cost":"1D4","armor_per_mp":"1D6","duration_hours":24,"cast_rounds":5,"stacking":False,"source_pdf_page":230},
    "shrivelling":{"name":"枯萎术","mp_cost":"chosen","san_cost":"mp//2","damage":"mp","pow_opposed":True,"source_pdf_page":235},
    "fist_yog_sothoth":{"name":"犹格索托斯之拳","mp_cost":"chosen","san_cost":"1D6","strength_per_mp":"2D10","range_surcharge_feet":30,"source_pdf_page":229},
    "summon":{"name":"召唤术","mp_cost":"chosen","san_cost":"1D4","success_percent_per_mp":10,"always_fail_at":96,"minutes_per_mp":5,"arrival_minutes":"2D10","source_pdf_page":236},
    "summon_servitor":{"name":"外神仆役召唤术","mp_cost":"chosen_multiple_3","san_cost":"3D4","success_percent_per_3mp":10,"always_fail_at":96,"enchantment_percent_per_pow":2,"source_pdf_page":237},
    "warding_stones":{"name":"守卫术","mp_cost":"stone_count","san_cost":"0","cast_rounds":5,"maximum_gap_yards":1,"source_pdf_page":238},
    "suggestion":{"name":"心理暗示术","mp_options":[5,10,15],"san_options":["1D3","2D3","3D3"],"cast_rounds":3,"pow_opposed":True,"source_pdf_pages":[232,233]},
}

# Travel formulas live separately so every special operation has an explicit,
# source-reviewed implementation rather than an arbitrary narrative effect.
from cockp.rules.spells.spell_travel import TRAVEL_SPELLS
SPELLS.update(TRAVEL_SPELLS)
from cockp.rules.spells.spell_contact import CONTACT_SPELLS
SPELLS.update(CONTACT_SPELLS)
from cockp.rules.spells.spell_rituals import RITUAL_SPELLS
SPELLS.update(RITUAL_SPELLS)
from cockp.rules.spells.spell_deep_attack import BINDING_SPELLS, BINDING_SPECIES
SPELLS.update(BINDING_SPELLS)


def _bounded_integer(value, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name}须为{minimum}至{maximum}的整数")
    return value


def summoning_result(mp_spent: int, roll: int, *, servitor: bool = False,
                     enchantment_pow: int = 0) -> dict:
    """Resolve a supplied audited d100; this pure function never draws dice.

    Incomplete groups of three MP give no extra servitor chance. The caller
    records all chosen MP as spent, and separately confirms the ritual's
    creature-specific material, location and astronomical prerequisites.
    """
    _bounded_integer(mp_spent,"MP",1,10000)
    _bounded_integer(roll,"D100结果",1,100)
    _bounded_integer(enchantment_pow,"已核验器物附魔POW",0,10000)
    chance=(mp_spent//3 if servitor else mp_spent)*10+enchantment_pow*2
    return {"success":roll<96 and roll<=chance,"threshold":min(95,chance),
            "raw_threshold":chance,"always_fail":roll>=96,"catastrophe":roll==100,
            "mp_spent":mp_spent,"source_pdf_page":237 if servitor else 236}


def expand_variable_spell(spell_id: str, *, mp_spent: int | None = None,
                          severity: int = 1, stone_count: int = 1,
                          distance_feet: float = 0, mp_roll: int | None = None,
                          enchantment_pow: int = 0, caster_int: int | None = None,
                          entity_id: str | None = None) -> dict:
    """Expand source numerical choices into a SpellDefinition-compatible dict.

    Dice strings in ``effect`` remain pending DiceService draws. ``mp_roll``
    must come from an audited 1D6 draw for cloud_memory. No resource, clock,
    authority, target, or random state is modified by this helper.
    """
    if spell_id not in VARIABLE_SPELLS: raise ValueError("未知变量法术")
    data=VARIABLE_SPELLS[spell_id]
    page=data.get("source_pdf_page",data.get("source_pdf_pages",[0])[0])
    minutes=0
    if spell_id in {"flesh_ward","shrivelling","fist_yog_sothoth","summon","summon_servitor"}:
        mp=_bounded_integer(mp_spent,"投入MP",1,10000)
    if spell_id=="flesh_ward":
        # The bounded dice parser is the shared safety limit, not a game rule.
        expression=f"{mp}D6";expression_bounds(expression)
        san="1D4"
        effect={"kind":"manual","operation":"ablative_armor","armor_expression":expression,
                "duration_hours":24,"cast_rounds":5,"stacking":False,"nonmagical_only":True}
    elif spell_id=="shrivelling":
        san=str(mp//2)
        effect={"kind":"damage","expression":str(mp),"pow_opposed":True}
    elif spell_id=="fist_yog_sothoth":
        if type(distance_feet) not in {int,float} or not math.isfinite(distance_feet) or distance_feet<0:
            raise ValueError("距离须为有限非负数")
        surcharge=max(0,math.ceil(distance_feet/30)-1)
        effective_mp=mp-surcharge
        if effective_mp<1: raise ValueError("支付距离消耗后须至少保留1MP产生力量")
        strength=f"{effective_mp*2}D10";expression_bounds(strength)
        san="1D6"
        effect={"kind":"manual","operation":"force_strike","strength_expression":strength,
                "range_surcharge_mp":surcharge,"opposed_target_attribute":"CON",
                "knockback_feet_formula":"max(0,spell_STR-target_SIZ)","requires_line_of_sight":True}
    elif spell_id in {"summon","summon_servitor"}:
        _bounded_integer(enchantment_pow,"已核验器物附魔POW",0,10000)
        servitor=spell_id=="summon_servitor"
        if servitor:
            if entity_id not in {None,"outer_servitor"}: raise ValueError("外神仆役召唤术不能改为其他物种")
            entity_id="outer_servitor"
        elif entity_id is not None:
            if not isinstance(entity_id,str) or entity_id not in BINDING_SPECIES or entity_id=="outer_servitor":
                raise ValueError("须指定已登记召唤物种；外神仆役使用summon_servitor")
            spell_id="summon_"+entity_id
        chance=(mp//3 if servitor else mp)*10+enchantment_pow*2
        san="3D4" if servitor else "1D4"
        minutes=5*mp
        if minutes>10080: raise ValueError("施法时间超出当前法术Schema的10080分钟限制，需分段仪式裁定")
        effect={"kind":"manual","operation":"summon","success_percent":min(95,chance),
                "raw_success_percent":chance,"always_fail_at":96,"catastrophe_at":100,
                "arrival_minutes":"2D10","binding_separate":True,
                "prerequisites_require_adjudication":True,"enchantment_pow":enchantment_pow,
                "entity_id":entity_id}
    elif spell_id=="suggestion":
        _bounded_integer(severity,"暗示危险程度",1,3)
        mp=[5,10,15][severity-1];san=["1D3","2D3","3D3"][severity-1]
        effect={"kind":"condition","condition":"suggested_action","pow_opposed":True,"cast_rounds":3,
                "severity":severity,"second_opposed_before_grave_harm":True,
                "duration_rounds_formula":"caster_INT","stronger_investigator_recheck_rounds":10,
                "eligible_target_requires_adjudication":True}
        if caster_int is not None:
            effect["duration_rounds"]=_bounded_integer(caster_int,"施法者INT",1,10000)
    elif spell_id=="cloud_memory":
        mp=_bounded_integer(mp_roll,"已审计MP成本D6结果",1,6);san="1D2"
        effect={"kind":"manual","operation":"cloud_specific_memory","pow_opposed":True,
                "specific_event_required":True,"cost_roll_expression":"1D6"}
    elif spell_id=="warding_stones":
        mp=_bounded_integer(stone_count,"石头数量",1,10000);san="0"
        effect={"kind":"condition","condition":"warding_stones_alarm","cast_rounds":5,
                "maximum_gap_yards":1,"stone_count":mp,"ends_when_stone_moved":True,"alerts_while_asleep":True}
    name=(BINDING_SPECIES[entity_id][0]+"召唤术") if entity_id and effect.get("operation")=="summon" else data["name"]
    return _spell(spell_id,name,mp,san,0,minutes,page,effect)


def weapon_special(weapon: Weapon, effect: str, **parameters) -> dict:
    """Registered numerical effects; unknown source-specific effects fail closed."""
    if effect not in weapon.special:
        raise ValueError("武器没有所声明特殊能力")
    if effect == "shotgun_ranges":
        distance = parameters["distance"]
        if distance < 0:
            raise ValueError("距离非法")
        return {"damage": "4D6" if distance <= 10 else "2D6" if distance <= 20 else "1D6" if distance <= 50 else "0", "impaling": False}
    if effect == "range_STR_feet":
        return {"range_feet": parameters["str"]}
    if effect == "fire_every_two_rounds":
        return {"available": parameters["round"] - parameters["last_fired_round"] >= 2}
    if effect == "double_fumble_range":
        return {"fumble_min": 91 if parameters["effective_threshold"] < 50 else 99}
    if effect in {"burning", "explosion_3_yards"}:
        from cockp.rules.rules import special_damage
        return special_damage("burning" if effect == "burning" else "explosion", **parameters)
    if effect == "grapple_ongoing_1D6":
        return {"ongoing_damage": "1D6", "escape_requires": "maneuver", "restricted_targets": "human_like"}
    return {"requires_manual_adjudication": True, "effect": effect, "source_pdf_page": weapon.source_pdf_page}
