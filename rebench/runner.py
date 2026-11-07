"""复位主循环：观测 -> 规划 -> 单步执行（含降级阶梯）-> 验收 -> 落盘。

三个不变量：
1. 任何单步失败都不会终止整轮（L4 标记后继续下一个物体）。
2. 每步结果立刻写 checkpoint，进程被杀后可续跑。
3. 只有 verify() 通过才输出 READY FOR NEXT RUN。

遮挡记忆（`--occlusion-memory`，默认开）：
目标被盒子挡住时，`judge()` 会给它 UNOBSERVABLE，而 `needs_action()` 只收
MISSING/WRONG_SLOT/MISPLACED —— 待复位队列里就没有它，机器人"什么都不做"。
这里加一层跨帧世界模型：看得见时记住三维位置，看不见但**视线确实被挡**时
判为 occluded（而不是消失），并在垂直走廊可行的前提下用记忆位置盲抓。
细节见 rebench/occlusion.py。
"""

import json
import os
import time
from typing import List, Optional

from .occlusion import (OCCLUDED, WorldModel, assess_pick,
                        check_vertical_approach, pick_strategy)
from .state import OK, UNOBSERVABLE, Bench, judge, needs_action

MAX_L1_RETRY = 2
OBJ_TIMEOUT_S = 40.0


def target_fields(tgt: Optional[dict]) -> dict:
    """从 world.target() 里挑出抓取要用的字段，别把整个 dict 混进决策里。"""
    if not tgt:
        return {}
    return {"pose": tgt.get("pose"), "quality": tgt.get("quality"),
            "age_s": tgt.get("age_s"), "uncertainty_m": tgt.get("uncertainty_m")}


class Runner:
    def __init__(self, backend, bench: Bench, ckpt_path: str, log_path: str,
                 mock: bool = False, occlusion_memory: bool = True):
        self.b = backend
        self.bench = bench
        self.ckpt_path = ckpt_path
        self.log_path = log_path
        self.mock = mock
        self.occlusion_memory = occlusion_memory
        self.world = WorldModel()
        self._warned_world = False
        self.state = self._load_ckpt()

    # ---------- checkpoint ----------
    def _load_ckpt(self) -> dict:
        if os.path.exists(self.ckpt_path):
            with open(self.ckpt_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"round": 0, "done": [], "history": []}

    def _save_ckpt(self) -> None:
        os.makedirs(os.path.dirname(self.ckpt_path), exist_ok=True)
        tmp = self.ckpt_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.ckpt_path)

    def _log(self, **kw) -> None:
        kw["t"] = round(time.time(), 3)
        kw["round"] = self.state["round"]
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(kw, ensure_ascii=False) + "\n")

    # ---------- 世界模型 ----------
    def _world_obs(self) -> dict:
        try:
            return self.b.observe_world()
        except NotImplementedError as exc:
            # 真机还没实现 observe_world：退回槽位级观测，记忆功能自动失效，
            # 但主流程照跑 —— 不能因为"新功能没接"让整轮复位停摆。
            # 只喊一次：每帧都刷屏反而没人看。
            if not self._warned_world:
                self._warned_world = True
                msg = ("[遮挡记忆] 后端没有实现 observe_world，三维位姿与障碍物拿不到，"
                       "遮挡目标会被静默放弃。实现要点见 docs/OCCLUSION_MEMORY.md")
                print(msg, flush=True)
                self._log(ev="observe_world_missing", detail=str(exc)[:200])
            return {}

    def _refresh_world(self, w_obs: dict) -> None:
        """把这一帧的观测灌进世界模型。**只有看得见时才更新位置。**"""
        for oid, info in (w_obs.get("objects") or {}).items():
            if info.get("visible") and info.get("pose"):
                self.world.see(oid, info["pose"], slot=info.get("slot"))
        self.world.set_obstacles(w_obs.get("obstacles") or [],
                                 w_obs.get("camera_pose"))

    def warm_memory(self) -> dict:
        """只观测、不动作：把当前能看见的目标位置记下来。

        真实时序就是这样：人先看见积木在桌上 → 再往上盖盒子 → 才轮到机器人动手。
        没有这一步，遮挡发生后系统手里没有任何位置可依据，"记得原位"无从谈起。
        """
        w_obs = self._world_obs()
        self._refresh_world(w_obs)
        seen = {oid: list(o.pose) for oid, o in self.world.objects.items()
                if o.pose is not None}
        self._log(ev="warm_memory", seen=seen, obstacles=len(self.world.obstacles))
        return seen

    def _plan_occluded(self, report: dict, w_obs: dict) -> dict:
        """对"看不见但记得"的物体做盲抓决策。返回 {obj_id: 决策}。"""
        out = {}
        if not self.occlusion_memory:
            return out
        for oid, r in report.items():
            if r["status"] != UNOBSERVABLE:
                continue
            info = (w_obs.get("objects") or {}).get(oid) or {}
            state = self.world.resolve(oid, slot_reason=info.get("slot_reason"))
            if state != OCCLUDED:
                self._log(ev="occlusion_check", obj=oid, state=state,
                          slot_reason=info.get("slot_reason"))
                continue
            tgt = self.world.target(oid)
            spec = self.bench.get(oid)
            width_m = (spec.width_mm / 1000.0) if spec else 0.04
            feas = (check_vertical_approach(tgt["pose"], self.world.obstacles, width_m)
                    if tgt and tgt.get("pose") else {"ok": False, "reason": "no_pose"})
            dec = pick_strategy(tgt, feas)
            self._log(ev="occlusion_plan", obj=oid, state=state, quality=tgt.get("quality") if tgt else None,
                      age_s=tgt.get("age_s") if tgt else None,
                      corridor=feas.get("reason"), action=dec["action"],
                      yaw_deg=feas.get("gripper_yaw_deg"), msg=dec.get("message"))
            if dec["action"] in ("blind_pick", "reobserve"):
                # reobserve 在 mock 里没法真挪手腕，按盲抓走，但日志里标明
                out[oid] = {**dec, **target_fields(tgt), "feasibility": feas}
        return out

    # ---------- 抓取原语 ----------
    def pick(self, obj_id: str, blind: Optional[dict] = None) -> dict:
        """抓一个物体。传 `blind`（含记忆位姿）就走"遮挡盲抓"分支。"""
        spec = self.bench.get(obj_id)
        target_w = spec.width_mm / 1000.0 - 0.006
        t0 = time.time()
        pose = (blind or {}).get("pose")
        blind_mode = pose is not None

        for attempt in range(1, MAX_L1_RETRY + 2):          # L1
            if time.time() - t0 > OBJ_TIMEOUT_S:
                return {"ok": False, "level": "L4", "reason": "timeout"}

            self.b.open_gripper(0.100, 5.0)
            # 高空横移对准 → 原地垂直下降。任何情况下都不许低空横移：
            # 离桌面 2 cm 平着挪会把物体扫飞（任务书 §7.7）。
            self.b.move_line(("pre", obj_id, attempt), speed=0.15)
            if blind_mode:
                self.b.move_line(("grasp_pose", pose), speed=0.03)
            else:
                self.b.move_line(("grasp", obj_id, attempt), speed=0.03)
            self.b.close_gripper(target_w, 12.0)
            w = self.b.gripper_width().width_m
            self.b.move_line(("lift", obj_id, attempt), speed=0.05)

            info = ((self._world_obs().get("objects") or {}).get(obj_id) or {})
            v = assess_pick(width_after=w, expected_width_m=target_w,
                            area_visible=bool(info.get("area_visible", True)),
                            slot_after=info.get("slot"))
            self._log(ev="pick_assess", obj=obj_id, attempt=attempt,
                      verdict=v["verdict"], width_m=round(w, 4), blind=blind_mode,
                      area_visible=info.get("area_visible"), slot=info.get("slot"))

            if not v["ok"]:
                self._log(ev="pick_fail", obj=obj_id, level="L1", attempt=attempt,
                          reason=v["reason"], detail=v["detail"])
                continue

            if v["verdict"] == "ok":
                self._log(ev="pick_ok", obj=obj_id, attempt=attempt,
                          width_m=round(w, 4), blind=blind_mode)
                return {"ok": True, "width_m": w, "verified": True,
                        "detail": v["detail"]}

            # unverified：夹住了但区域仍不可观测。动作层面拿起来了，
            # **但不算验收通过** —— 交给复位后的完整观测去确认。
            self._log(ev="pick_unverified", obj=obj_id, attempt=attempt,
                      width_m=round(w, 4), blind=blind_mode, detail=v["detail"])
            return {"ok": True, "width_m": w, "verified": False,
                    "reason": "unverified_under_occlusion", "detail": v["detail"]}

        # L2 / L3 的实现在此处扩展：换抓取面、换手、换视角重观测
        self._log(ev="pick_fail", obj=obj_id, level="L4", reason="ladder_exhausted")
        return {"ok": False, "level": "L4", "reason": "ladder_exhausted"}

    def _slot_is_empty(self, obj_id: str) -> bool:
        obs = self.b.observe()
        seen = obs.get(obj_id) or {}
        return seen.get("slot") is None

    # ---------- 一轮复位 ----------
    def run_round(self) -> dict:
        self.state["round"] += 1
        rid = self.state["round"]
        done = set(self.state.get("done", []) if self.state.get("round_active") else [])

        w_obs = self._world_obs()
        self._refresh_world(w_obs)
        report0 = judge(self.bench, self.b.observe())
        todo = [o for o in needs_action(report0) if o not in done]
        blind = self._plan_occluded(report0, w_obs)
        for oid in blind:
            if oid not in todo:
                todo.append(oid)          # 记得位置、走廊可行 → 也去抓
        self._log(ev="round_start", todo=todo, report=report0, blind=list(blind))
        self.state["round_active"] = True
        self._save_ckpt()

        for obj_id in todo:
            r = self.pick(obj_id, blind=blind.get(obj_id))
            if r["ok"]:
                if self.mock:
                    self.b.simulate_pick(obj_id, self.bench.get(obj_id).home_slot)
                done.add(obj_id)
                self.state["done"] = sorted(done)
                self._save_ckpt()
            else:
                self._log(ev="obj_deferred", obj=obj_id, level=r.get("level"), reason=r.get("reason"))

        # 验收 gate
        report1 = judge(self.bench, self.b.observe())
        ready = all(v["status"] == OK for v in report1.values())
        self.state["round_active"] = False
        self.state["done"] = []
        self.state["history"].append({"round": rid, "ready": ready, "report": report1})
        self._save_ckpt()
        self._log(ev="round_end", ready=ready, report=report1)
        return {"round": rid, "ready": ready, "report": report1}


def print_gate(result: dict) -> None:
    print(f"\n{'=' * 46}")
    print(f"  第 {result['round']} 轮复位结果")
    print(f"{'=' * 46}")
    for oid, r in result["report"].items():
        mark = "OK  " if r["status"] == "OK" else "FAIL"
        print(f"  [{mark}] {oid:14s} {r['status']:12s} {r['detail']}")
    verdict = "READY FOR NEXT RUN" if result["ready"] else "NOT READY — 存在未复位项"
    print(f"\n  > {verdict}\n")
