/**
 * news-etl TTS relay — Cloudflare Worker
 *
 * 为什么存在：news-etl 本机（中国住宅 IP）与 GitHub Actions（数据中心 IP）
 * 直连微软 readaloud 端点都会被 403。这个 Worker 把微软调用挪到 Cloudflare
 * 的非中国边缘节点发出，本地/CI 只需 HTTPS POST 到本 Worker 即可，403 一次性解决。
 *
 * 与 wangwangit/tts 的关键区别：它走的是 cognitiveservices REST 音频端点
 * （只回音频、丢逐词时间戳）。本项目复刻 edge-tts 的 readaloud WebSocket 协议，
 * 因此**保留 WordBoundary 逐词时间戳**，与 news-etl/app/tts.py 的对齐逻辑无缝衔接。
 *
 * 部署（需 Cloudflare 账号 + $5/月 Workers Paid，免免费档 10ms CPU 限制）：
 *   npm i -g wrangler && wrangler login
 *   wrangler secret put AUTH_KEY        # 设一个随机串，news-etl 用 TTS_RELAY_KEY 带上
 *   wrangler deploy
 * 然后在 news-etl 环境设置：
 *   TTS_RELAY_URL=https://news-etl-tts-relay.<subdomain>.workers.dev
 *   TTS_RELAY_KEY=<同一个 AUTH_KEY>
 */

const TRUSTED_CLIENT_TOKEN = "6A5AA1D4EAFF4E9FB37E23D68491D6F4";
const TICKS_PER_SECOND = 10_000_000;
const MP3_BITRATE_BPS = 48_000;
const DEFAULT_VOICE = "en-US-EmmaMultilingualNeural";

// ----- 工具 -----

function uuid() {
  return crypto.randomUUID().replace(/-/g, "");
}

function jsDate() {
  const d = new Date();
  const days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${days[d.getUTCDay()]} ${months[d.getUTCMonth()]} ${pad(d.getUTCDate())} ` +
    `${d.getUTCFullYear()} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())} ` +
    `GMT+0000 (Coordinated Universal Time)`
  );
}

function xmlEscape(s) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

function unescapeXml(s) {
  return s
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&amp;/g, "&");
}

// edge-tts 会剔除这些控制字符，否则微软报错
function cleanText(s) {
  return s.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g, " ");
}

/**
 * Sec-MS-GEC：用 BigInt 精确计算（避免 1e17 量级的浮点精度丢失）。
 * 算法与 edge-tts/drm.py 字节级一致：
 *   ticks = unix_now + WIN_EPOCH; 向下取整到 5 分钟; * 1e7;
 *   sha256(f"{ticks}{TRUSTED_CLIENT_TOKEN}").hexdigest().upper()
 */
async function generateSecMsGec() {
  const WIN_EPOCH = 11644473600n;
  let ticks = BigInt(Math.floor(Date.now() / 1000)) + WIN_EPOCH;
  ticks -= ticks % 300n; // 向下取整到 5 分钟
  ticks = ticks * 10000000n; // 1e9 / 100 = 1e7（100ns tick）
  const str = ticks.toString() + TRUSTED_CLIENT_TOKEN;
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(str));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("").toUpperCase();
}

function parseHeaders(str) {
  const h = {};
  for (const line of str.split("\r\n")) {
    if (!line) continue;
    const idx = line.indexOf(":");
    if (idx === -1) continue;
    h[line.slice(0, idx).trim().toLowerCase()] = line.slice(idx + 1).trim();
  }
  return h;
}

function base64FromBytes(bytes) {
  let bin = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
}

// ----- 协议消息 -----

function speechConfigMsg() {
  return (
    `X-Timestamp:${jsDate()}\r\n` +
    "Content-Type:application/json; charset=utf-8\r\n" +
    "Path:speech.config\r\n\r\n" +
    '{"context":{"synthesis":{"audio":{"metadataoptions":{' +
    '"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"true"' +
    '},"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n'
  );
}

function ssmlMsg(text, voice) {
  const escaped = xmlEscape(cleanText(text));
  const ssml =
    `<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='en-US'>` +
    `<voice name='${voice}'>` +
    `<prosody pitch='+0Hz' rate='+0%' volume='+0%'>${escaped}</prosody>` +
    `</voice></speak>`;
  return (
    `X-RequestId:${uuid()}\r\n` +
    "Content-Type:application/ssml+xml\r\n" +
    `X-Timestamp:${jsDate()}Z\r\n` + // 末尾 Z 是 edge-tts 故意的（微软 bug）
    "Path:ssml\r\n\r\n" +
    ssml
  );
}

async function buildWssUrl(ver) {
  const gec = await generateSecMsGec();
  const major = ver.split(".")[0];
  const secVer = `1-${ver}`;
  return (
    "wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1" +
    `?TrustedClientToken=${TRUSTED_CLIENT_TOKEN}` +
    `&ConnectionId=${uuid()}` +
    `&Sec-MS-GEC=${gec}` +
    `&Sec-MS-GEC-Version=${secVer}`
  );
}

function wsHeaders(major) {
  const UA =
    `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ` +
    `(KHTML, like Gecko) Chrome/${major}.0.0.0 Safari/537.36 Edg/${major}.0.0.0`;
  return {
    "User-Agent": UA,
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Accept-Language": "en-US,en;q=0.9",
    Pragma: "no-cache",
    "Cache-Control": "no-cache",
    Origin: "chrome-extension://jdiccldimpdaibmpdkjnbmckianbfold",
    "Sec-WebSocket-Version": "13",
    "Sec-CH-UA": `" Not;A Brand";v="99", "Microsoft Edge";v="${major}", "Chromium";v="${major}"`,
    "Sec-CH-UA-Mobile": "?0",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    Cookie: `muid=${uuid()};`,
  };
}

// 按 4096 字节上限分片（与 edge-tts 一致），优先在句末/空格断，避免截断 UTF-8 / XML 实体
function splitByBytes(text, maxBytes = 4096) {
  const enc = new TextEncoder();
  const cleaned = cleanText(text);
  const parts = cleaned.split(/(?<=[.!?。！？\n])\s*/);
  const grouped = [];
  let cur = "";
  for (const p of parts) {
    const candidate = cur ? cur + " " + p : p;
    if (enc.encode(candidate).length > maxBytes && cur) {
      grouped.push(cur);
      cur = p;
    } else {
      cur = candidate;
    }
  }
  if (cur) grouped.push(cur);

  const out = [];
  for (let c of grouped) {
    if (enc.encode(c).length <= maxBytes) {
      out.push(c);
      continue;
    }
    // 单句超长，硬切（极少触发）
    let start = 0;
    while (start < c.length) {
      let end = c.length;
      while (end > start && enc.encode(c.slice(start, end)).length > maxBytes) end--;
      out.push(c.slice(start, end));
      start = end;
    }
  }
  return out.filter((c) => c.trim().length > 0);
}

// ----- 单分片合成（捕获音频 + 逐词边界） -----

function synthesizeChunk(textChunk, voice, offsetComp, ver) {
  return new Promise(async (resolve, reject) => {
    const url = await buildWssUrl(ver);
    const major = ver.split(".")[0];
    let ws;
    try {
      ws = new WebSocket(url, [], { headers: wsHeaders(major) });
    } catch (e) {
      return reject(e);
    }
    ws.binaryType = "arraybuffer";

    const audioChunks = [];
    const boundaries = [];
    let chunkAudioBytes = 0;
    let audioReceived = false;
    let err = null;

    const handleText = (textData) => {
      const sep = textData.indexOf("\r\n\r\n");
      if (sep === -1) return;
      const headers = parseHeaders(textData.slice(0, sep));
      const body = textData.slice(sep + 4);
      const path = headers["path"];
      if (path === "audio.metadata") {
        let json;
        try {
          json = JSON.parse(body);
        } catch {
          return;
        }
        for (const m of json.Metadata || []) {
          if (m.Type === "WordBoundary" || m.Type === "SentenceBoundary") {
            const d = m.Data || {};
            const raw = d.text && d.text.Text !== undefined ? d.text.Text : d.text || "";
            boundaries.push({
              offset: (d.Offset || 0) + offsetComp,
              duration: d.Duration || 0,
              text: unescapeXml(raw),
            });
          }
        }
      }
      // turn.end / response / turn.start 忽略，靠 close 事件收尾
    };

    const handleBinary = (buf) => {
      if (buf.byteLength < 2) return;
      const dv = new DataView(buf);
      const headerLength = dv.getUint16(0);
      if (headerLength > buf.byteLength) return;
      const headerStr = new TextDecoder().decode(new Uint8Array(buf, 2, headerLength));
      const dataBytes = new Uint8Array(buf, 2 + headerLength);
      const headers = parseHeaders(headerStr);
      if (headers["path"] !== "audio") return;
      const ct = headers["content-type"];
      if (ct && ct !== "audio/mpeg") return;
      if (dataBytes.length === 0) return;
      audioReceived = true;
      chunkAudioBytes += dataBytes.length;
      audioChunks.push(dataBytes);
    };

    const watchdog = setTimeout(() => {
      err = new Error("TTS synthesis timed out");
      try {
        ws.close();
      } catch {}
    }, 180000);

    ws.addEventListener("open", () => {
      try {
        ws.send(speechConfigMsg());
        ws.send(ssmlMsg(textChunk, voice));
      } catch (e) {
        clearTimeout(watchdog);
        reject(e);
      }
    });

    ws.addEventListener("message", (ev) => {
      try {
        if (typeof ev.data === "string") handleText(ev.data);
        else handleBinary(ev.data);
      } catch (e) {
        err = e;
      }
    });

    ws.addEventListener("error", (e) => {
      clearTimeout(watchdog);
      reject(new Error("WebSocket error: " + (e.message || e)));
    });

    ws.addEventListener("close", () => {
      clearTimeout(watchdog);
      if (err) return reject(err);
      if (!audioReceived) return reject(new Error("No audio received from TTS service"));
      const newOffsetComp = offsetComp + Math.floor((chunkAudioBytes * 8 * TICKS_PER_SECOND) / MP3_BITRATE_BPS);
      resolve({ audioChunks, boundaries, newOffsetComp });
    });
  });
}

// ----- 整段合成（多分片 + 跨片 offset 补偿） -----

async function synthesize(text, voice, env) {
  const ver = env.CHROMIUM_VERSION || "155.0.4283.45";
  const chunks = splitByBytes(text, 4096);
  const audioParts = [];
  const allBoundaries = [];
  let offsetComp = 0;
  for (const ch of chunks) {
    const r = await synthesizeChunk(ch, voice, offsetComp, ver);
    for (const p of r.audioChunks) audioParts.push(p);
    for (const b of r.boundaries) allBoundaries.push(b);
    offsetComp = r.newOffsetComp;
  }
  let total = 0;
  for (const p of audioParts) total += p.length;
  const merged = new Uint8Array(total);
  let off = 0;
  for (const p of audioParts) {
    merged.set(p, off);
    off += p.length;
  }
  return { audio: merged, boundaries: allBoundaries };
}

function cors() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, x-api-key",
  };
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors() });
    }
    if (request.method === "GET") {
      return new Response(JSON.stringify({ ok: true, engine: "edge-tts-relay" }), {
        headers: { "Content-Type": "application/json", ...cors() },
      });
    }
    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405, headers: cors() });
    }

    // 鉴权：设了 AUTH_KEY 就必须带 x-api-key，否则谁都能烧额度
    const authKey = env.AUTH_KEY;
    if (authKey) {
      const provided = request.headers.get("x-api-key") || "";
      if (provided !== authKey) {
        return new Response(JSON.stringify({ error: { message: "unauthorized", code: "auth" } }), {
          status: 401,
          headers: { "Content-Type": "application/json", ...cors() },
        });
      }
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return new Response(JSON.stringify({ error: { message: "invalid json" } }), {
        status: 400,
        headers: { "Content-Type": "application/json", ...cors() },
      });
    }

    const text = (body.text || "").toString();
    const voice = (body.voice || DEFAULT_VOICE).toString();
    if (!text.trim()) {
      return new Response(JSON.stringify({ error: { message: "empty text" } }), {
        status: 400,
        headers: { "Content-Type": "application/json", ...cors() },
      });
    }

    try {
      const result = await synthesize(text, voice, env);
      return new Response(
        JSON.stringify({
          audio_base64: base64FromBytes(result.audio),
          boundaries: result.boundaries,
          format: "audio-24khz-48kbitrate-mono-mp3",
        }),
        { headers: { "Content-Type": "application/json", ...cors() } }
      );
    } catch (e) {
      return new Response(
        JSON.stringify({ error: { message: String((e && e.message) || e), code: "tts_error" } }),
        { status: 500, headers: { "Content-Type": "application/json", ...cors() } }
      );
    }
  },
};
