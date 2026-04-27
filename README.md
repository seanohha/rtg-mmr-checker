# RTG MMR Checker

`config.json`에 등록된 League of Legends 소환사들의 MMR을
[rankedkings.com](https://rankedkings.com/mmr-checker)에서 가져와
히스토리를 CSV로 기록하고, 소환사별 카드 + 통합 비교 라인 차트로 보여주는
Streamlit 앱.

## 구성

```
streamlit_app.py     Streamlit UI (카드 + Plotly 차트)
mmr_fetcher.py       rankedkings JSON API 호출 (HTTP 202 큐잉 재시도)
history.py           CSV 읽기/쓰기 (UTF-8 BOM, Excel 호환)
config.json          소환사 목록
mmr_history.csv      MMR 히스토리 (자동 생성, append)
.streamlit/config.toml  다크 테마
requirements.txt     의존성 (streamlit, plotly, httpx)
```

## 로컬 실행

Python 3.10+ 필요.

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

## Streamlit Community Cloud 배포

1. 이 저장소를 GitHub에 push.
2. [share.streamlit.io](https://share.streamlit.io) 접속 → "New app".
3. Repository: `<your-username>/rtg-mmr-checker`, Branch: `main`,
   Main file path: `streamlit_app.py`.
4. Deploy 클릭.

> **주의**: Streamlit Community Cloud는 파일시스템이 ephemeral 입니다.
> 앱 재시작 시 `mmr_history.csv` 가 저장소의 commit된 상태로 되돌아갑니다.
> 영구 히스토리가 필요하면 GitHub Actions 같은 외부 트리거로 주기적으로
> commit 하거나, 외부 스토리지(Sheets, S3, Postgres 등)를 연결하세요.

## 사용

- **Refresh All** 버튼 → 전체 소환사 갱신. 진행률 + 경과 시간이 표시됨.
- 각 카드의 **Refresh** 버튼 → 해당 소환사 1명만 갱신.
- 통합 비교 차트의 범례를 클릭하면 라인 토글, 더블클릭하면 단일 라인만 표시.
- 결과는 `mmr_history.csv`에 한 줄씩 append.

## config.json 필드

```json
{
  "summoners": [
    {
      "name": "SasimySean",
      "tag": "4174",
      "region": "KR",
      "queue_type": "Ranked Flex",
      "owner": "Sean"
    }
  ],
  "log_file": "mmr_history.csv"
}
```

- `region`: `NA, EUW, EUNE, OCE, BR, RU, TR, LAN, LAS, ME, JP, KR, SEA, TW, VN`
- `queue_type`: `Ranked Solo, Ranked Flex, Normal Draft, Swift Play, ARAM`
- `owner`: 카드 그루핑용 라벨 (자유 문자열)
- `log_file`: 히스토리 파일 경로 (기본 `mmr_history.csv`)

## 동작 방식

`https://api.rankedkings.com/lol-mmr/v2/check/{REGION}/{NAME%23TAG}/{QUEUE}/false`
를 호출.
- `200 + status:"SUCCESS"` 면 MMR/등급/actual MMR을 CSV에 기록.
- `202` (캐시 미스로 큐잉됨) 이면 3초 간격으로 최대 8회 폴링.
- 그 외 (존재하지 않는 소환사 등) 는 실패로 처리하고 CSV에 기록하지 않음.
