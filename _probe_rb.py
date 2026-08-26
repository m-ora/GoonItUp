import re
import urllib.request

ua = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
url = "https://realbooru.com/index.php?page=post&s=view&id=1004206"
req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "text/html"})
with urllib.request.urlopen(req, timeout=25) as resp:
    html = resp.read().decode("utf-8", errors="replace")
print("len", len(html))
for pat in ["images/", "samples/", "img.realbooru", "source=", "id=\"image\"", ".mp4", ".webm"]:
    print(pat, html.lower().count(pat.lower()))
idx = html.find("images/")
print("images idx", idx)
if idx >= 0:
    print(html[idx - 80 : idx + 180])
idx = html.find('id="image"')
print("image tag", idx)
if idx >= 0:
    print(html[idx : idx + 300])
# video
for m in re.finditer(r'(https?://[^"\']+\.(?:mp4|webm|jpg|jpeg|png|gif))', html, re.I):
    print("media", m.group(1)[:160])
