"""Build hms_zh.json (HMS / print_error code -> Chinese text) used by bridge.py.

Sources:
  * Bambu cloud DB  https://e.bambulab.com/query.php?lang=zh-cn  (all models, generic)
  * Bambu Studio    resources/hms/hms_zh-cn_<serial prefix>.json  (model specific, e.g. 094 = H2D)
Output: {"default": {"hms": {...}, "err": {...}}, "<prefix>": {only entries that differ}}
Run occasionally: python build_hms.py [--lang zh-cn] [--out hms_zh.json]
"""
import argparse, json, re, urllib.request

UA = {"User-Agent": "Mozilla/5.0"}
GH = "https://api.github.com/repos/bambulab/BambuStudio/contents/resources/hms"
RAW = "https://raw.githubusercontent.com/bambulab/BambuStudio/master/resources/hms/"


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return json.load(r)


def table(doc, lang):
    d = doc["data"]
    return {"hms": {x["ecode"].upper(): x["intro"].strip() for x in d.get("device_hms", {}).get(lang, [])},
            "err": {x["ecode"].upper(): x["intro"].strip() for x in d.get("device_error", {}).get(lang, [])}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="zh-cn")
    ap.add_argument("--out", default="hms_zh.json")
    a = ap.parse_args()
    out = {"default": table(get(f"https://e.bambulab.com/query.php?lang={a.lang}"), a.lang)}
    names = [f["name"] for f in get(GH)]
    for n in names:
        m = re.fullmatch(rf"hms_{re.escape(a.lang)}_(\w+)\.json", n)
        if not m:
            continue
        t = table(get(RAW + n), a.lang)
        for k in ("hms", "err"):
            for code, txt in t[k].items():
                out["default"][k].setdefault(code, txt)
        out[m.group(1)] = t
    for p, t in out.items():
        if p != "default":
            for k in ("hms", "err"):
                t[k] = {c: s for c, s in t[k].items() if out["default"][k].get(c) != s}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    print({p: {k: len(v) for k, v in t.items()} for p, t in out.items()})


if __name__ == "__main__":
    main()
