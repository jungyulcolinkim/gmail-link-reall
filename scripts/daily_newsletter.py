#!/usr/bin/env python3
"""정열님의 매일 아침 뉴스레터 — GitHub Actions에서 실행."""
import json
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

import anthropic
import requests

# 한국 시간(KST = UTC+9)
KST = timezone(timedelta(hours=9))
WEEKDAYS = ['월', '화', '수', '목', '금', '토', '일']


def kst_today_kor() -> str:
    now = datetime.now(KST)
    return f"{now.year}년 {now.month:02d}월 {now.day:02d}일 ({WEEKDAYS[now.weekday()]})"


def three_days_ago_iso() -> str:
    return (datetime.now(KST) - timedelta(days=3)).strftime('%Y-%m-%d')


def today_iso() -> str:
    return datetime.now(KST).strftime('%Y-%m-%d')


# ----------------------------------------------------------------------------
# 1) Claude API + web_search 로 뉴스 큐레이션 → JSON
# ----------------------------------------------------------------------------

CURATION_PROMPT_TEMPLATE = """너는 매일 아침 한국어 뉴스레터를 작성하는 큐레이터다. 오늘은 {today_kor} (오늘 = {today_iso}).

# 작업 흐름
1. web_search 도구로 각 카테고리에 해당하는 뉴스를 검색한다 (검색어에 "오늘", "{today_iso}", "최근" 같은 키워드 적극 활용).
2. **각 검색 결과의 발행일자를 반드시 확인**한다. 검색 결과 스니펫이나 본문에서 날짜를 추출하지 못하면 그 기사는 사용하지 않는다.
3. 발행일이 **{three_days_ago} 이상 {today_iso} 이하** 범위 안에 있는 기사만 후보로 선정한다.
4. 검증 통과한 기사로 JSON을 구성한다.
5. 마지막 응답엔 **JSON 한 덩어리만** 출력한다 (다른 설명·코드 펜스 금지).

# 카테고리 (각 3건씩, 총 12건)

🌐 국내외 뉴스 — 한국 정치/사회/외교 + 국제 주요 이슈 (전쟁/분쟁, 외교, 지정학)
💻 IT/테크 — 빅테크 발표, 반도체, 신기술, 사이버보안
🤖 AI — **Claude(Anthropic), ChatGPT/OpenAI, Gemini/Google AI 모델 업데이트는 반드시 1건 이상**, 그 외 AI 스타트업/정책/한국 AI 동향
💰 금융/경제 — 국제유가(WTI/브렌트), 환율(원/달러·엔/달러), 미국·한국 증시, Fed/한국은행 금리

# 절대 규칙 (위반 시 기사 폐기)

## 1. 날짜 검증 (가장 중요!)
- **허용 범위**: {three_days_ago} ~ {today_iso} (3일 이내 발행)
- 검색 결과에 "X일 전", "어제", "오늘" 같은 상대 표기만 있고 절대 날짜를 확인할 수 없으면 → **사용 금지**
- 검색 결과에 발행일이 명시되지 않으면 → **사용 금지**
- 발행일이 {three_days_ago} 이전이면 → **사용 금지** (4월 기사, 작년 기사 등 절대 포함 금지)
- "Updated" 표시가 있고 원래 발행일이 오래된 기사 → **사용 금지** (재탕 기사 거름)

## 2. 매체 검증
**허용 매체만 사용**:
- 국내: 연합뉴스, 조선일보, 중앙일보, 동아일보, 한겨레, 경향신문, 한국일보, 국민일보, 서울신문, KBS, MBC, SBS, JTBC, YTN, MBN, 채널A, TV조선, 매일경제, 한국경제, 머니투데이, 이데일리, 파이낸셜뉴스, 서울경제, 헤럴드경제, 디지털타임스, 전자신문, 블로터, IT조선, 지디넷코리아
- 글로벌: Reuters, Bloomberg, AP, AFP, BBC, NHK, Al Jazeera, FT, WSJ, NYT, Washington Post, The Guardian, The Economist, CNN, CNBC, Nikkei, TechCrunch, The Verge, Wired, Ars Technica, MIT Technology Review
- AI 1차 출처: Anthropic blog, OpenAI blog, Google AI/DeepMind blog, Microsoft AI blog, Meta AI blog
- **금지**: 개인 블로그, 네이버 블로그/카페, 티스토리, 광고성 보도자료, 출처 모를 사이트

## 3. 다양성
- 한 카테고리 안에 동일 사건의 후속 보도가 몰리지 않게 분산
- 한국어 1~2줄 요약 (50~120자)

# 출력 JSON 스키마

각 item의 `date` 필드는 **YYYY-MM-DD 형식**으로 정확히 적어라 (예: "{today_iso}"). MM/DD 같은 축약 금지.

{{
  "categories": [
    {{
      "key": "domestic",
      "title": "🌐 국내외 뉴스",
      "color": "#1a73e8",
      "items": [
        {{"title": "...", "summary": "...", "source": "...", "date": "YYYY-MM-DD", "url": "..."}}
      ]
    }},
    {{"key": "tech", "title": "💻 IT / 테크", "color": "#10b981", "items": [...]}},
    {{"key": "ai", "title": "🤖 AI", "color": "#8b5cf6", "items": [...]}},
    {{"key": "finance", "title": "💰 금융 / 경제", "color": "#f59e0b", "items": [...]}}
  ]
}}

# 최종 자체 검증 (JSON 출력 전 반드시!)
JSON을 만든 직후, 출력 전에 머릿속으로 다음을 확인한다:
- [ ] 모든 item의 `date`가 {three_days_ago} ~ {today_iso} 범위 안에 있는가?
- [ ] 발행일이 의심스러운 기사가 1개라도 있으면, 그 기사를 빼고 같은 카테고리에서 다른 날짜 검증된 기사로 대체했는가?
- [ ] 모든 item의 `source`가 위 허용 매체 목록에 있는가?

위 검증을 통과해야만 JSON을 출력한다. 검색을 충분히 한 뒤, **마지막 메시지에는 위 JSON만** 출력해라.
"""


def curate_news() -> dict:
    client = anthropic.Anthropic()
    prompt = CURATION_PROMPT_TEMPLATE.format(
        today_kor=kst_today_kor(),
        today_iso=today_iso(),
        three_days_ago=three_days_ago_iso(),
    )
    print('Calling Claude API with web_search…', flush=True)
    response = client.messages.create(
        model='claude-sonnet-4-5-20250929',
        max_tokens=8000,
        tools=[{
            'type': 'web_search_20250305',
            'name': 'web_search',
            'max_uses': 15,
        }],
        messages=[{'role': 'user', 'content': prompt}],
    )
    print(f'  stop_reason={response.stop_reason}', flush=True)

    text_blocks = [b for b in response.content if getattr(b, 'type', '') == 'text']
    if not text_blocks:
        raise RuntimeError('Claude 응답에 text 블록 없음')
    final_text = text_blocks[-1].text.strip()

    # 코드 펜스가 있으면 제거
    if final_text.startswith('```'):
        final_text = re.sub(r'^```(?:json)?\s*\n', '', final_text)
        final_text = re.sub(r'\n```\s*$', '', final_text)

    # JSON 파싱
    try:
        return json.loads(final_text)
    except json.JSONDecodeError as e:
        # 첫 { 부터 마지막 } 까지만 추출 시도
        start = final_text.find('{')
        end = final_text.rfind('}')
        if start >= 0 and end > start:
            return json.loads(final_text[start:end + 1])
        raise RuntimeError(f'JSON 파싱 실패: {e}\n원본: {final_text[:500]}')


# ----------------------------------------------------------------------------
# 2) JSON → HTML 뉴스레터 (Gmail iOS 다크모드 최적화 템플릿)
# ----------------------------------------------------------------------------

def html_escape(s: str) -> str:
    return (
        s.replace('&', '&amp;')
         .replace('<', '&lt;')
         .replace('>', '&gt;')
         .replace('"', '&quot;')
    )


def build_html(news_data: dict, today_kor: str) -> str:
    parts = [f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>오늘의 뉴스레터</title>
</head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Segoe UI',sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f5f5f5" style="background:#f5f5f5;">
<tr><td align="center" style="padding:20px 10px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;border-radius:12px;overflow:hidden;">
<tr>
<td bgcolor="#0f172a" style="background:#0f172a;padding:36px 24px;text-align:center;">
<p style="margin:0 0 8px;font-size:13px;color:#cbd5e1 !important;letter-spacing:1.5px;font-weight:500;">{html_escape(today_kor)}</p>
<h1 style="margin:0;font-size:28px;font-weight:800;color:#fbbf24 !important;line-height:1.3;">☀️ 오늘의 뉴스레터</h1>
<p style="margin:8px 0 0;font-size:14px;color:#e2e8f0 !important;">정열님의 아침 브리핑</p>
</td>
</tr>"""]

    for cat in news_data.get('categories', []):
        color = cat.get('color', '#1a73e8')
        title = html_escape(cat.get('title', ''))
        parts.append(f"""
<tr><td bgcolor="#ffffff" style="background:#ffffff;padding:24px 20px 8px;">
<h2 style="margin:0 0 16px;font-size:20px;font-weight:700;color:#0f172a;border-bottom:3px solid {color};padding-bottom:8px;display:inline-block;">{title}</h2>
</td></tr>""")
        for item in cat.get('items', []):
            t = html_escape(item.get('title', ''))
            s = html_escape(item.get('summary', ''))
            src = html_escape(item.get('source', ''))
            d = html_escape(item.get('date', ''))
            u = item.get('url', '#')
            parts.append(f"""
<tr><td bgcolor="#ffffff" style="background:#ffffff;padding:0 20px 12px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
<tr><td bgcolor="#f8fafc" style="background:#f8fafc;padding:16px;border-radius:8px;border-left:4px solid {color};">
<h3 style="margin:0 0 8px;font-size:16px;font-weight:700;color:#0f172a;line-height:1.4;">{t}</h3>
<p style="margin:0 0 10px;font-size:14px;color:#475569;line-height:1.6;">{s}</p>
<p style="margin:0;font-size:13px;color:#64748b;">{src} · {d} · <a href="{u}" style="color:{color};text-decoration:none;font-weight:600;">원문 보기 →</a></p>
</td></tr>
</table>
</td></tr>""")

    parts.append("""
<tr>
<td bgcolor="#0f172a" style="background:#0f172a;padding:24px 20px;text-align:center;">
<p style="margin:0;font-size:12px;color:#94a3b8 !important;">매일 아침 7시 · 정열님을 위한 큐레이션</p>
</td>
</tr>
</table>
</td></tr>
</table>
</body>
</html>""")
    return ''.join(parts)


# ----------------------------------------------------------------------------
# 3) Gmail SMTP 발송
# ----------------------------------------------------------------------------

def send_gmail(html_body: str, today_kor: str) -> None:
    user = os.environ['GMAIL_USER']
    password = os.environ['GMAIL_APP_PASSWORD'].replace(' ', '')

    msg = MIMEMultipart('alternative')
    msg['From'] = formataddr(('☀️ 뉴스레터', user))
    msg['To'] = user
    msg['Subject'] = f'☀️ 오늘의 뉴스레터 - {today_kor}'
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))

    print('Sending Gmail via SMTP…', flush=True)
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL('smtp.gmail.com', 465, context=context) as server:
        server.login(user, password)
        server.send_message(msg)
    print('  ✅ Gmail 발송 완료', flush=True)


# ----------------------------------------------------------------------------
# 4) 카카오톡 "나에게 보내기" (REST API)
# ----------------------------------------------------------------------------

def refresh_kakao_token() -> tuple[str, str | None]:
    """access_token 발급. (access, new_refresh or None)."""
    rest_key = os.environ['KAKAO_REST_API_KEY']
    secret = os.environ['KAKAO_CLIENT_SECRET']
    refresh_token = os.environ['KAKAO_REFRESH_TOKEN']

    print('Refreshing Kakao token…', flush=True)
    resp = requests.post(
        'https://kauth.kakao.com/oauth/token',
        data={
            'grant_type': 'refresh_token',
            'client_id': rest_key,
            'client_secret': secret,
            'refresh_token': refresh_token,
        },
        timeout=15,
    )
    data = resp.json()
    if 'error' in data:
        raise RuntimeError(f'카카오 토큰 갱신 실패: {data}')
    access = data['access_token']
    new_refresh = data.get('refresh_token')
    if new_refresh:
        # GitHub Secrets는 자동 갱신 불가 → 사용자가 수동 업데이트할 수 있도록 로그 출력
        print(
            f'  ⚠️ Kakao refresh_token 이 회전됐습니다. '
            f'GitHub → Settings → Secrets → KAKAO_REFRESH_TOKEN 을 새 값으로 업데이트하세요. '
            f'(끝 4자리: …{new_refresh[-4:]})',
            flush=True,
        )
    return access, new_refresh


def send_kakao(message: str) -> None:
    access_token, _ = refresh_kakao_token()

    template_object = {
        'object_type': 'text',
        'text': message,
        'link': {
            'web_url': 'https://jungyulcolinkim.github.io/Gmail-Landing/',
            'mobile_web_url': 'https://jungyulcolinkim.github.io/Gmail-Landing/',
        },
        'button_title': '지메일 열기',
    }

    print('Sending KakaoTalk message…', flush=True)
    resp = requests.post(
        'https://kapi.kakao.com/v2/api/talk/memo/default/send',
        headers={'Authorization': f'Bearer {access_token}'},
        data={'template_object': json.dumps(template_object, ensure_ascii=False)},
        timeout=15,
    )
    result = resp.json()
    if result.get('result_code') != 0:
        raise RuntimeError(f'카카오 발송 실패: {result}')
    print('  ✅ 카카오톡 발송 완료', flush=True)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> int:
    today_kor = kst_today_kor()
    print(f'\n📰 {today_kor} 뉴스레터 시작\n' + '=' * 50, flush=True)

    # 1. 큐레이션
    try:
        news_data = curate_news()
        total = sum(len(c.get('items', [])) for c in news_data.get('categories', []))
        print(f'  ✅ 뉴스 {total}건 수집 완료', flush=True)
    except Exception as e:
        print(f'  ❌ 뉴스 큐레이션 실패: {e}', flush=True)
        return 1

    # 1-b. 날짜 범위 검증 (3일 이내인지 안전망 체크)
    valid_from = three_days_ago_iso()
    valid_to = today_iso()
    out_of_range = []
    for cat in news_data.get('categories', []):
        for item in cat.get('items', []):
            d = item.get('date', '')
            # YYYY-MM-DD 형식이어야만 비교
            if not re.match(r'^\d{4}-\d{2}-\d{2}$', d):
                out_of_range.append((cat.get('title', ''), item.get('title', ''), d))
                continue
            if d < valid_from or d > valid_to:
                out_of_range.append((cat.get('title', ''), item.get('title', ''), d))
    if out_of_range:
        print(f'  ⚠️ 날짜 범위({valid_from} ~ {valid_to}) 벗어난 기사 {len(out_of_range)}건:', flush=True)
        for cat_title, item_title, d in out_of_range:
            print(f'     - [{cat_title}] {item_title} ({d})', flush=True)

    # 2. HTML 생성
    html = build_html(news_data, today_kor)
    print(f'  ✅ HTML 생성 완료 ({len(html):,} bytes)', flush=True)

    # 3. Gmail 발송
    gmail_ok = False
    try:
        send_gmail(html, today_kor)
        gmail_ok = True
    except Exception as e:
        print(f'  ❌ Gmail 실패: {e}', flush=True)

    # 4. 카카오톡 발송
    kakao_ok = False
    try:
        kakao_msg = '☀️ 오늘의 뉴스레터가 도착했어요!\n\n📧 지메일 앱에서 확인하세요'
        send_kakao(kakao_msg)
        kakao_ok = True
    except Exception as e:
        print(f'  ❌ 카카오톡 실패: {e}', flush=True)

    # 5. 최종 보고
    print('\n' + '=' * 50)
    print(f'📧 Gmail: {"✅" if gmail_ok else "❌"}')
    print(f'💬 KakaoTalk: {"✅" if kakao_ok else "❌"}')
    print(f'📊 뉴스: {total}건')
    return 0 if (gmail_ok and kakao_ok) else 1


if __name__ == '__main__':
    sys.exit(main())
