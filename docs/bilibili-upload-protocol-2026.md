# bilibili 新上传协议实测（2026-09-09 浏览器抓包）

## 背景
老库 bilibili-api-python 17.4.2 的 preupload/ugcfx/bup 流程被新服务端全面拒绝：
- `GET member.bilibili.com/preupload?profile=ugcfx/bup` → 406 + code 601「上传过快」（伪装话术）
- 全套清洗矩阵见本次会话；上流库已停更（最后 commit "owari" 2026-07）

## 新流程（真实浏览器 POST 冲击）
1. `POST https://member.bilibili.com/upload/file`
   body: `{"profile":"fxmeta/bup","name":"file_meta.txt"}`
   resp.data: `{uri: "upos://fxmetalf/<key>.txt", reqs:[{method:"PUT", url:"https://jscs-luffy-upcdn{bldsa|tx...}.bilivideo.com/fxmetalf/<key>.txt?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=...&X-Amz-Date=...&X-Amz-Expires=900&X-Amz-SignedHeaders=host&x-id=PutObject&X-Amz-Signature=...", cdn_name:"..."}, ...]}`
   即：**S3 预签名直传**，多 CDN 候选。
2. `POST https://member.bilibili.com/upload/multipart/new`
   body: `{"profile":"ugcfx/bup","init_params":{"meta_upos_uri":"upos://fxmetalf/<key>.txt"},"name":"<file>","size":<bytes>}`
   → 分片会话；限流时返回 code 244001「上传过快」（无害挂起态还带 v_voucher）
3. PUT 内容到预签名 URL（大文件分片）
4. 收尾 + `POST /x/vu/web/add/v3`（投稿提交，含 csrf=bili_jct）

## 限流规律
- code 601(旧)/244001(新) = 账号级上传限流，**每次尝试都会重置静默计时窗**
- 静默 4.5h 后单次 probe 可过；连续尝试会持续顶线
- 唯一出路：长静默后单发 + 慢速节奏（10-15min/条）
