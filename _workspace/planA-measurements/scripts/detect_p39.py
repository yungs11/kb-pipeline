"""layout 블록 기하로 'p39형 부서진 다이어그램'을 포착할 수 있는가 (오프라인)."""
import json, os, collections

SP = os.path.dirname(os.path.abspath(__file__))
VIS = {"image", "chart", "figure"}
TEXTISH = {"text", "paragraph_title"}

# 정답 라벨: True=다이어그램 페이지(VL 필요), False=아님
PAGES = {
    "abl_p39_파싱흐름": True,    # 3스윔레인 순서도 (layout 이 놓침)
    "abl_p40_불릿본문": False,
    "abl_p20_간지": False,
    "lico_p9_성능차트": False,   # 표+본문
    "def_p6_흐름도+표": False,   # 표 페이지
    "def_p5_흐름도": True,       # image 로 이미 잡힘(대조)
    "abl_p14_아키텍처": True,    # image 로 이미 잡힘(대조)
    "abl_p31_RAG흐름": True,     # image 로 이미 잡힘(대조)
    "abl_p33_리랭킹차트": True,
    "lico_p3_간트": True,
    # --- 확대 표본 (2차) ---
    "abl_p13_지식그래프": True,
    "abl_p16_병렬수행체계": True,
    "abl_p17_PoC흐름": True,
    "abl_p22_TBox트리": True,
    "abl_p28_전역흐름": True,
    "abl_p35_스크린샷": False,
    "abl_p36_성능차트": True,
    "abl_p38_KStudio": True,
    "abl_p4_방사형": True,
    "abl_p21_CQ흐름": True,
    "abl_p6_표": False,
    "abl_p10_표": False,
    "def_p4_표": False,
    "def_p13_표스샷": False,
    "def_p7_표2": False,
    "lico_p11_대형표": False,
}


def feats(name):
    d = json.load(open(f"{SP}/L_{name}.json"))
    lay = d["layout"][0]
    W, H = lay["width"], lay["height"]
    blocks = lay["blocks"]
    vis = [b for b in blocks if b["block_label"] in VIS]
    tb = [b for b in blocks if b["block_label"] in TEXTISH]
    tables = [b for b in blocks if b["block_label"] == "table"]

    # y구간이 겹치는 블록 쌍 비율 = 나란히 배치(스윔레인/컬럼) 정도
    pairs = ov = 0
    for i in range(len(tb)):
        for j in range(i + 1, len(tb)):
            a, b = tb[i]["block_bbox"], tb[j]["block_bbox"]
            pairs += 1
            inter = min(a[3], b[3]) - max(a[1], b[1])
            if inter > 0.5 * min(a[3] - a[1], b[3] - b[1]):
                ov += 1
    yov = ov / pairs if pairs else 0.0

    # 좌정렬 컬럼 수 (x0 를 페이지폭 5% 로 양자화)
    cols = len({round(b["block_bbox"][0] / (W * 0.05)) for b in tb})

    # 텍스트 블록이 덮는 면적 비율
    cov = sum((b[2]-b[0])*(b[3]-b[1]) for b in
              [x["block_bbox"] for x in tb]) / (W * H)
    # 짧은 텍스트 비율 (노드 라벨처럼 짧은가)
    lens = [len((b.get("block_content") or "").strip()) for b in tb]
    short = sum(1 for l in lens if l <= 30) / len(lens) if lens else 0.0
    return dict(vis=len(vis), tbl=len(tables), ntext=len(tb),
                yov=round(yov, 3), cols=cols, cov=round(cov, 3),
                short=round(short, 2),
                medlen=sorted(lens)[len(lens)//2] if lens else 0)


print(f"{'page':<20} {'정답':<5} vis tbl ntext  yov  cols   cov short medlen")
for name, truth in PAGES.items():
    f = feats(name)
    mark = "DIAG" if truth else "  -"
    print(f"{name:<20} {mark:<5} {f['vis']:>3} {f['tbl']:>3} {f['ntext']:>5} "
          f"{f['yov']:>5} {f['cols']:>4} {f['cov']:>6} {f['short']:>5} {f['medlen']:>6}")
