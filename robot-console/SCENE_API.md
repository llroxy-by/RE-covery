# 桌面识图共享接口

GET http://127.0.0.1:8765/api/scene 返回同一份原子更新的识图快照。
字段：scene_id（每帧唯一版本）、captured_at（收到相机帧的时间，非硬件曝光时间）、published_at、age_ms、stale、source、tracks 或 objects。默认超过3秒即 ok=false；无数据不能当作空桌面。

启动原有 robot_vision.py 相机识别程序后，每次成功识别自动发布。Web每500毫秒读取缓存，不重复调用模型。服务端需重启以加载本次代码；识别程序也需重启。没有运行识别生产者时页面显示未就绪。模型和相机配置沿用原项目，本接口不自动打开真机或启动模型。

抓取规划读取 /api/scene，保留scene_id，再将它作为 pickplace_session.py 的batch顶层scene_id传入。执行批次开始时会检查同一版本及新鲜度，不符合则拒绝整个批次。每个物体应单独一个批次；批次内按同一份快照完成动作，下一物体重新识别规划。可用 GET /api/scene?scene_id=原版本 验证版本。

此检查约束的是新接入、显式携带scene_id的批次；旧手动批次未自动转换为视觉抓取。二维tracks不是三维抓取坐标，仍须原有深度定位和碰撞检查，禁止直接将bbox作为运动坐标。

其它识别程序可import scene_store并调用publish(result,captured_at,source)，不要另写Web专属识图缓存。所有本机进程须使用相同ROBOT_SCENE_FILE（默认/tmp/galbot-scene.json）；当前仅支持单一识图生产者。未接入该发布函数的临时Gemini脚本不会自动同步。

## 跨机器部署（推荐）

A机器跑相机/识图，B机器跑共享识图API，C机器跑网页，D机器跑抓取；可以分别部署，也可以合并。共享文件只作为B服务器内部存储，不要求跨机器共享目录。

B设置一个足够长的随机 `ROBOT_SCENE_TOKEN`（所有后端使用同一个值，勿提交Git），**不设置** ROBOT_SCENE_URL，然后运行：

```sh
python3 vision-rescue/scene_http.py --host 0.0.0.0 --port 8790
```

A、C、D后端都设置：

```sh
export ROBOT_SCENE_URL=http://B的局域网IP:8790/api/scene
export ROBOT_SCENE_TOKEN=事先约定的密钥
```

A的robot_vision.py自动POST结果；D的带scene_id抓取批次自动GET核对；C的控制台后端把同源 `/api/scene` 请求代理到B，浏览器不需要知道密钥。不要在B设置ROBOT_SCENE_URL指向自身，否则会循环请求。独立API同时支持带Bearer密钥的GET和POST，仅提供识图数据，不包含运动接口。其他网页服务可在后端用相同方式代理，勿把密钥放浏览器JS。正式外网部署请通过HTTPS反向代理，局域网地址根据现场网络填写。各机时钟需同步；采集至使用默认3秒有效期，网络耗时也计入。

POST字段最少为 `{"ok":true,"captured_at":Unix秒,"objects":[]}`；也可传tracks。服务端生成并返回scene_id，消费者以这个返回值为准。失败不会回退到本机缓存。GET可带 `?scene_id=版本`，过期/版本不一致时ok=false。该机制保证共享快照版本一致，不代表机械臂运动期间场景不会变化。
