import json, glob, os, collections

SP = os.path.dirname(os.path.abspath(__file__))
VIS = {"image", "chart", "figure", "figure_title", "chart_title"}
order = ["def_p5_흐름도", "def_p6_흐름도+표", "abl_p14_아키텍처", "abl_p31_RAG흐름", "abl_p39_파싱흐름",
         "abl_p33_리랭킹차트", "abl_p20_간지", "abl_p40_불릿본문", "lico_p9_성능차트", "lico_p3_간트"]

for name in order:
    p = f"{SP}/L_{name}.json"
    if not os.path.exists(p):
        continue
    d = json.load(open(p))
    lay = d["layout"][0]
    W, H = lay["width"], lay["height"]
    det = lay["detection"]
    blocks = lay["blocks"]
    cnt = collections.Counter(b["label"] for b in det)
    print(f"\n=== {name}  page={W}x{H}  text_len={len(d.get('text') or '')}")
    print("   labels:", dict(cnt))
    for b in blocks:
        if b["block_label"] in VIS:
            x0, y0, x1, y1 = b["block_bbox"]
            area = (x1 - x0) * (y1 - y0) / (W * H)
            c = (b.get("block_content") or "").replace("\n", " ")
            print(f"   [{b['block_label']}] area={area:.1%} bbox={b['block_bbox']} content({len(c)}): {c[:180]}")
