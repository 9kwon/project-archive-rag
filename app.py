"""연구과제 문서 RAG — 웹 UI.

    streamlit run app.py

내부망 서버에서 띄울 때:
    streamlit run app.py --server.address 0.0.0.0 --server.port 8501
"""

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent / "src"))

from ingest import load_config  # noqa: E402
from project import get_profile  # noqa: E402
from search import DOC_WEIGHT, Searcher  # noqa: E402

_P = get_profile()

st.set_page_config(page_title="과제 문서 검색", page_icon="📑", layout="wide")

YEAR_LABELS = {0: "전체", **{y: _P.year_label(y) for y in _P.years}}


# 가끔 쓰는 도구라 앱이 떠 있는 내내 모델을 붙들고 있을 이유가 없다.
# 임베딩 모델은 첫 검색 때 올라가고(Searcher가 지연 로딩한다),
# 한 시간 동안 아무도 쓰지 않으면 캐시가 풀려 메모리를 돌려준다.
# 그 뒤 첫 검색은 모델을 다시 올리느라 10~20초 걸린다.
@st.cache_resource(ttl=3600, show_spinner="검색 인덱스를 불러오는 중…")
def get_searcher() -> Searcher:
    return Searcher(load_config())


@st.cache_data(show_spinner=False)
def perf_summary(proj_year: int) -> dict | None:
    """성과 DB 조회 결과. DB가 없으면 None."""
    import perf_query as pq

    try:
        con = pq.connect(load_config())
    except FileNotFoundError:
        return None
    s = pq.year_summary(con, proj_year)
    con.close()
    return s


@st.cache_data(show_spinner=False)
def indicator_rows() -> list[dict]:
    """지표별 연차 목표·실적 (엑셀 성능지표 시트와 같은 내용)."""
    import sqlite3

    import perf_query as pq
    from export_excel import normalize_indicator
    from perf_table import indicator_org

    try:
        con = pq.connect(load_config())
    except FileNotFoundError:
        return []

    data: dict[str, dict] = {}
    rows = con.execute(
        "SELECT indicator, unit, proj_year, target, NULL actual FROM year_target "
        "UNION ALL SELECT indicator, unit, proj_year, year_target, year_actual FROM year_actual"
    ).fetchall()
    for r in rows:
        name = normalize_indicator(r["indicator"])
        e = data.setdefault(name, {"지표": name, "단위": r["unit"] or "", "담당": indicator_org(name)})
        y = r["proj_year"]
        if r["target"] and not e.get(f"{y}차 목표"):
            e[f"{y}차 목표"] = r["target"]
        if r["actual"]:
            e[f"{y}차 실적"] = r["actual"]
    con.close()

    cols = ["지표", "단위", "담당"] + [f"{y}차 {k}" for y in range(1, 6) for k in ("목표", "실적")]
    return [{c: e.get(c, "") for c in cols} for e in data.values()]


def source_badge(meta: dict) -> str:
    bits = [meta.get("doc_type", "")]
    if y := meta.get("proj_year"):
        bits.append(f"{y}차년도")
    if org := meta.get("org"):
        bits.append(org)
    return " · ".join(b for b in bits if b)


def render_hits(hits, numbered: bool = False):
    for i, h in enumerate(hits, 1):
        label = f"[{i}] " if numbered else ""
        with st.expander(f"{label}{h.source()}  —  {source_badge(h.meta)}", expanded=False):
            st.caption(f"검색 경로: {h.via} · 점수 {h.score:.4f}")
            st.text(h.text)


# ── 사이드바 ────────────────────────────────────────────────
with st.sidebar:
    st.header("검색 범위")
    year = st.selectbox("연차", list(YEAR_LABELS), format_func=YEAR_LABELS.get)
    doc_type = st.selectbox("문서 종류", ["전체", *sorted(DOC_WEIGHT)])
    org = st.selectbox("기관", ["전체", *_P.org_codes])

    st.divider()
    st.header("검색 설정")
    k = st.slider("근거 개수", 3, 15, 8)
    mode = st.radio("검색 방식", ["hybrid", "vector", "bm25"], horizontal=True)
    weight = st.checkbox("문서 권위도 반영", value=True,
                         help="연차보고서·성과확인서를 회의록보다 우선한다")

filters: dict = {}
if year:
    filters["proj_year"] = year
if doc_type != "전체":
    filters["doc_type"] = doc_type
if org != "전체":
    filters["org"] = org

# ── 본문 ────────────────────────────────────────────────────
st.title("연구과제 문서 검색")
st.caption(f"{_P.title} · {_P.period} · {_P.n_years}개년 과제 문서 검색과 근거 기반 답변")

tab_qa, tab_search, tab_perf = st.tabs(["질의응답", "근거 검색", "연차별 성과"])

with tab_qa:
    st.markdown("검색된 근거만으로 답한다. 자료에 없으면 없다고 답하며, 문장마다 근거 번호를 단다.")
    q = st.text_input("질문", placeholder="예: 4차년도 논문과 특허 성과는 각각 몇 건인가",
                      key="qa_input")
    if st.button("질문하기", type="primary", disabled=not q):
        from generate import ask

        try:
            with st.spinner("근거를 찾고 답변을 작성하는 중…"):
                answer, hits = ask(q, filters or None, k=k)
        except RuntimeError as e:
            st.error(f"{e}\n\nOllama가 실행 중인지 확인하세요: `ollama serve`")
        else:
            st.markdown(answer)
            st.divider()
            st.subheader(f"근거 {len(hits)}건")
            render_hits(hits, numbered=True)

with tab_search:
    st.markdown("LLM 없이 검색 결과만 본다. 어느 검색(vector/bm25)이 찾았는지 함께 표시된다.")
    q2 = st.text_input("검색어", placeholder="예: 3차년도 성능지표 달성률", key="search_input")
    if q2:
        with st.spinner("검색 중… (첫 검색은 임베딩 모델을 올리느라 10~20초 걸린다)"):
            hits = get_searcher().search(
                q2, k=k, filters=filters or None, mode=mode, weight=weight
            )
        if not hits:
            st.info("결과가 없다. 필터를 넓혀보라.")
        else:
            st.caption(f"{len(hits)}건 · 필터 {filters or '없음'}")
            render_hits(hits)

with tab_perf:
    rows = indicator_rows()
    if not rows:
        st.warning("성과 DB가 없다. `python src/perf_table.py`를 먼저 실행하라.")
    else:
        st.subheader("성능지표 연차별 목표 · 실적")
        only_home = st.checkbox(f"{_P.home_org} 담당 지표만", value=True)
        shown = [r for r in rows if not only_home or r["담당"] == _P.home_org]
        st.dataframe(shown, use_container_width=True, hide_index=True)
        st.caption("원본 보고서 표에서 추출한 값이다. 빈 칸은 해당 자료에 기재가 없는 것이다.")

        st.divider()
        y = st.selectbox("연차 상세", list(_P.years), format_func=lambda v: YEAR_LABELS[v],
                         key="perf_year")
        s = perf_summary(y)
        if s:
            import perf_query as pq

            st.markdown(pq.format_summary(s))
