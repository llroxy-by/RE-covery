# 使用说明

已准备 G1 SDK 1.10.0 官方固定坐标演示；不是自动识别抓取成品。
原始源码保存在 official_example6.py.txt；example6_pick_and_place.py 仅移除缺依赖时自动 pip 安装的行为。

## 只读检查（不初始化机器人、不运动）
```sh
cd ~/recovery-grasp-example
python3 -c "import ast,pathlib; ast.parse(pathlib.Path('example6_pick_and_place.py').read_text()); print('syntax OK')"
```

## 现场演示启动
先由现场人员核对源码中的初始姿态、目标位置、夹爪参数和场地；只有确认这些固定坐标适用于现场后才运行：
```sh
cd ~/recovery-grasp-example
env PYTHONPATH=/data/galbot/lib LD_LIBRARY_PATH=/data/galbot/lib python3 -B -u example6_pick_and_place.py
```
程序询问 y/n。输入 n 退出；输入 y 后会初始化并移动腿、头、双臂，随后左手按固定坐标抓取、抬升、撤回，有定位时底盘导航 0.1m，再放置。不是仅抬手。
固定抓取 TCP 位姿为 base_link 下 [0.4,0.3,0.65,0,0,0,1]，位置单位米，四元数顺序 xyzw；接近段 0.05m。不能用移动物品来代替完整标定与碰撞检查。
夹爪设定张开 0.1m、闭合 0.02m；闭合命令成功不证明夹住物体。

运行前保持实体急停可用，现场人员观察整个运动范围。Ctrl+C 触发程序清理，但不能保证底层运动立即停止；需要立即停止时使用实体急停。不要并行运行其他运动控制程序。

## 自动识别仍缺什么
源码 detect_object 的结果和 grasp_pose 都是占位值；必须接入真实左腕 RGB-D 检测、标定及 TCP 抓取姿态，并完善初始化/夹爪错误处理，才能用于真实目标。详见 README.md。

本次只上传并静态检查，没有启动此脚本，没有验证抓取成功。没有安装或升级 SDK。
