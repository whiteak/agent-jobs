// Upgrade 6a (# U06): the screenshot driver for a sandbox that has Node Playwright
// instead of Python Playwright. Same spec and same answer as visionshot.py: the board
// pipes it in with a here-doc, passes the check spec in VISION_SPEC_B64 and reads the
// PNGs back as one base64 JSON line on stdout, so nothing is written in the project.
import http from "node:http";
import fs from "node:fs";
import path from "node:path";

const MARK = "VISIONSHOT_JSON";
const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css",
               ".json": "application/json", ".png": "image/png", ".svg": "image/svg+xml",
               ".jpg": "image/jpeg", ".ico": "image/x-icon", ".woff2": "font/woff2" };

function emit(out) {
  process.stdout.write("\n" + MARK + " " + Buffer.from(JSON.stringify(out)).toString("base64") + "\n");
}

function serve(folder) {
  return http.createServer((req, res) => {
    const rel = decodeURIComponent((req.url || "/").split("?")[0]).replace(/^\/+/, "");
    const p = path.resolve(folder, rel === "" ? "index.html" : rel);
    if (!p.startsWith(path.resolve(folder) + path.sep)) { res.writeHead(403); res.end(); return; }
    fs.readFile(p, (err, data) => {
      if (err) { res.writeHead(404); res.end("not found"); return; }
      res.writeHead(200, { "Content-Type": MIME[path.extname(p).toLowerCase()] || "application/octet-stream" });
      res.end(data);
    });
  }).listen(0, "127.0.0.1");
}

async function doStep(page, step, tout) {
  if (step.click !== undefined) await page.click(step.click, { timeout: tout });
  else if (step.type !== undefined || step.fill !== undefined)
    await page.fill(step.type ?? step.fill, String(step.text ?? ""), { timeout: tout });
  else if (step.key !== undefined) await page.keyboard.press(String(step.key));
  else if (step.wait !== undefined) await page.waitForTimeout(Math.max(0, Number(step.wait) || 0));
  else if (step.wait_for !== undefined) await page.waitForSelector(step.wait_for, { timeout: tout });
  else return "unknown step";
  return null;
}

const out = { shots: [], steps: [], url: "", error: null };

// Playwright is in the image only if Jason put it there. Say that plainly - if the import
// simply failed the board would only see "the screenshot program did not answer".
let chromium = null;
try {
  ({ chromium } = await import("playwright"));
} catch (ex) {
  out.error = "Playwright for Node is not installed in the sandbox image " +
              "(npm install playwright && npx playwright install chromium)";
  out.no_browser = true;
  emit(out);
  process.exit(0);
}

let srv = null;
try {
  const spec = JSON.parse(Buffer.from(process.env.VISION_SPEC_B64 || "", "base64").toString("utf8"));
  const size = (spec.size || [1280, 800]).slice(0, 2);
  const tout = Number(spec.step_timeout_ms) || 10000;
  const folder = process.cwd();
  const openSpec = String(spec.open || "index.html").trim();
  if (!/^https?:\/\//.test(openSpec)) srv = serve(folder);
  const port = srv ? srv.address().port : null;
  let url = openSpec;
  if (/^https?:\/\//.test(url) && port !== null) url = url.replace("<port>", String(port));
  else url = "http://127.0.0.1:" + port + "/" + openSpec.replace(/^\/+/, "");
  out.url = url;

  const shots = [];
  const browser = await chromium.launch({ args: ["--no-sandbox", "--disable-dev-shm-usage"] });
  const page = await browser.newPage({ viewport: { width: Number(size[0]), height: Number(size[1]) } });
  try {
    await page.goto(url, { waitUntil: "load", timeout: 30000 });
    await page.waitForTimeout(400);
  } catch (ex) {
    out.error = "the page did not open: " + ex.message;
  }
  for (let i = 0; i < (spec.steps || []).length; i++) {
    const step = spec.steps[i];
    if (step && step.shot !== undefined) {
      shots.push([String(step.shot || ("step" + i)), await page.screenshot({ type: "png" })]);
      continue;
    }
    const err = step && typeof step === "object" ? await doStep(page, step, tout) : "not a step";
    out.steps.push({ step: i, what: JSON.stringify(step).slice(0, 200), error: err });
  }
  shots.push(["final", await page.screenshot({ type: "png" })]);
  await browser.close();
  for (const [name, png] of shots) out.shots.push({ name, b64: png.toString("base64") });
} catch (ex) {
  out.error = "the screenshot failed: " + ex.message;
} finally {
  if (srv) srv.close();
}
emit(out);
