import os
from dataclasses import dataclass
from typing import Callable
from pathlib import Path
import requests
import base64
import json
from dotenv import load_dotenv
import xml.etree.ElementTree as ET
from urllib.parse import quote

load_dotenv()             

# ── ① 약속: 성공도 실패도 같은 모양 ──────────────────────────
def ok(data):
    return {"ok": True, "data": data}

def fail(code, message):
    return {"ok": False, "error": {"code": code, "message": message}}

# 실패 코드
#   BAD_ARGUMENT    입력이 규격과 다르다 (빈 값 · 형식 틀림 · 모르는 인자)
#   INVALID_KEY     키가 없거나 틀렸다
#   NOT_FOUND       찾는 대상이 없다 (없는 식별자)
#   NO_DATA         조회는 됐지만 결과가 비었다
#   UPSTREAM_ERROR  외부 API 가 응답하지 않았다 (시간 초과 · 서버 오류)

# ── ② Tool 명함: 이름 · 설명 · 입력 규격 · 함수 ──────────────
@dataclass
class Tool:
    name: str             # 이름표
    description: str      # 언제 쓰는지 (모델이 읽는다)
    parameters: dict      # 입력 규격 (JSON Schema)
    func: Callable        # 실제로 일하는 함수

    def run(self, **kwargs):
        try:
            return self.func(**kwargs)
        except TypeError:
            return fail("BAD_ARGUMENT", "입력값이 규약과 다릅니다.")
        except Exception:
            return fail("UPSTREAM_ERROR", "Tool 실행 중 오류입니다.")

# ── ③ 외부 API Tool —─────────────────
API_URL = "https://gms.ssafy.io/gmsapi/generativelanguage.googleapis.com/v1beta/interactions"
KEY_NAME = "MY_API_KEY"  
HOSPITAL_KEY_NAME = "HOSPITAL_API_KEY" 
HOSPITAL_API_URL = "HOSPITAL_API_URL"                 

def read_image(image_path: str):
    if not image_path:
        return fail("BAD_ARGUMENT", "검진표 이미지 경로를 입력해 주세요.")
    
    path = Path(image_path)
    if not path.is_file():
        return fail("NOT_FOUND", "이미지 파일을 찾을 수 없습니다.")

    try:
        image_bytes = path.read_bytes()
    except OSError:
        return fail("READ_ERROR", "이미지를 읽을 수 없습니다.")

    key = os.getenv(KEY_NAME)
    if not key:
        return fail("INVALID_KEY", f"{KEY_NAME}가 없습니다.")
    
    mime_types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
    mime_type = mime_types.get(path.suffix.lower())
    if not mime_type:
        return fail("BAD_ARGUMENT", "PNG 또는 JPG 이미지를 사용해 주세요.")

    prompt = (
        "검진표에서 과거병력, 검사결과, 생활습관, 확인필요를 추출하세요. "
        "이 네 필드를 키로 갖는 JSON 객체를 반환하고 각 값은 배열로 작성하세요. "
        "검사결과에는 사진에 있는 항목명, 측정값, 단위, 체크된 판정을 기록하세요. "
        "참고범위를 측정값으로 기록하지 마세요. 생활습관에는 체크된 항목만 넣으세요. "
        "흐리거나 체크 여부가 불분명한 항목은 확인필요에 넣으세요."
        "과거 병력에는 과거 병력 칸의 병명만 넣고, 약물 치료와 합치지 마세요."
    )
    payload = {
        "model": "gemini-3.7-flash",
        "input": [
            {"type": "text", "text": prompt},
            {"type": "image", "mime_type": mime_type,
             "data": base64.b64encode(image_bytes).decode("ascii")},
        ],
        "response_format": {"type": "text", "mime_type": "application/json"},
        "store": False,
    }

    headers = {"x-goog-api-key": key}

    try:
        res = requests.post(API_URL, headers=headers, json=payload, timeout=60)
        if res.status_code in (401, 403):
            return fail("GMS_ERROR", "GMS 키와 모델 접근 권한을 확인해 주세요.")
        res.raise_for_status()
        body = res.json()
    except requests.RequestException:
        return fail("UPSTREAM_ERROR", "GMS 호출 또는 응답 처리에 실패했습니다.")

    parts = [part for step in body.get("steps", [])
             if step.get("type") == "model_output"
             for part in step.get("content", [])]
    text = "".join(part["text"] for part in parts if part.get("type") == "text")
    if not text:
        return fail("NO_DATA", "검진표 판독 결과가 없습니다.")

    try:
        checkup = json.loads(text)
    except json.JSONDecodeError:
        return fail("INVALID_RESPONSE", "판독 결과를 JSON으로 읽을 수 없습니다.")

    fields = ("과거병력", "검사결과", "생활습관", "확인필요")
    if not isinstance(checkup, dict) or not all(
        isinstance(checkup.get(field), list) for field in fields
    ):
        return fail("INVALID_RESPONSE", "판독 결과의 필드 형식을 확인해 주세요.")

    return ok({field: checkup[field] for field in fields})

#입력 -> 공공데이터 과 코드로 
DEPARTMENT_CODES = {
    "내과": "D001",
    "신경과": "D003",
    "신경외과": "D009",
    "가정의학과": "D022",
}

def search_hospitals(region : str , department : str , page_no: int = 1):
    if type(page_no) is not int or page_no < 1:
        return fail("BAD_ARGUMENT", "페이지 번호는 1 이상의 정수여야 합니다.")
    
    if not department:
        return fail("NO_DEP", "department는 필수입니다.")
    
    key = os.getenv(HOSPITAL_KEY_NAME)
    api = os.getenv(HOSPITAL_API_URL)

    if not key:
        return fail("INVALID_KEY", f"{HOSPITAL_KEY_NAME}가 없습니다.")
    
    area = region.strip().split(maxsplit=1)
    if len(area) != 2:
        return fail("BAD_ARGUMENT", "지역을 시도, 시군구 형태로 적어 주세요.")
    
    sido, sigungu = area
    department_code = DEPARTMENT_CODES.get(department.strip())
    if not department_code:
        return fail("BAD_ARGUMENT", "지원하지 않는 진료과입니다.")
    
    page_size = 10
    params = {
    "serviceKey": key,      # 공공데이터포털 인증키
    "Q0": sido,             # "대전광역시"
    "Q1": sigungu,          # "서구"
    "QD": department_code,  # 신경과에 해당하는 코드
    "pageNo": page_no,
    "numOfRows": page_size,
}
    try:
        res = requests.get(api, params=params, timeout=10)
        res.raise_for_status()
        root = ET.fromstring(res.content)
    except requests.RequestException:
        return fail("UPSTREAM_ERROR", "병원 API를 호출하지 못했습니다.")
    except ET.ParseError:
        return fail("INVALID_RESPONSE", "병원 API 응답을 XML로 읽을 수 없습니다.")

    if root.find("cmmMsgHeader") is not None:
        return fail("UPSTREAM_ERROR", "공공데이터포털 인증키와 활용승인 상태를 확인해 주세요.")

    if root.findtext("./header/resultCode") != "00":
        return fail("UPSTREAM_ERROR", "병원 API에서 정상 결과를 받지 못했습니다.")

    
    hospitals = []
    for item in root.findall("./body/items/item"):
        hospital_id = item.findtext("hpid")
        hospital_name = item.findtext("dutyName")
        if hospital_id and hospital_name:
            hospitals.append({"id": hospital_id, "name": hospital_name})

    if not hospitals:
        return fail("NO_DATA", f"{region}에서 {department} 병원을 찾지 못했습니다.")

    total_text = root.findtext("./body/totalCount", default="").strip()
    if not total_text.isdecimal():
        return fail("INVALID_RESPONSE", "응답에서 전체 병원 수를 확인할 수 없습니다.")
    total_count = int(total_text)

    next_page = None
    if page_no * page_size < total_count:
        next_page = page_no + 1

    return ok({
        "items": hospitals,
        "page_no": page_no,
        "total_count": total_count,
        "next_page": next_page,
    })


def get_hospital_detail(id : str):

    detail_url = (
        "https://apis.data.go.kr/B552657/HsptlAsembySearchService/"
         "getHsptlBassInfoInqire"        
    )

    if not id:
        return fail("BAD_ARGUMENT", "id는 필수입니다.")
    key = os.getenv(HOSPITAL_KEY_NAME)
    if not key:
        return fail("INVALID_KEY", f"{HOSPITAL_KEY_NAME}가 없습니다.")

    params = {
        "serviceKey" : key,
        "HPID" : id 
    }

    try:
        res = requests.get(detail_url, params=params,   
                           timeout=10)
        res.raise_for_status()
        root = ET.fromstring(res.content)

    except requests.RequestException:            
        return fail("UPSTREAM_ERROR", "병원 API를 부르지 못했습니다.")
    except ET.ParseError:
        return fail("INVALID_RESPONSE", "병원 상세 응답을 XML로 읽을 수 없습니다.")

    if root.findtext("./header/resultCode") != "00":
        return fail("UPSTREAM_ERROR", "병원 API에서 정상 결과를 받지 못했습니다.")
    
    items = root.find("./body/items/item")               
    if items is None:
        return fail("NO_DATA", f"'{id}'에 해당하는 병원이 없습니다.")

    name = items.findtext("dutyName", "").strip()
    lat = items.findtext("wgs84Lat", "").strip()
    lon = items.findtext("wgs84Lon", "").strip()
    direction_url = None

    if name and lat and lon:
        direction_url = f"https://map.kakao.com/link/to/{quote(name, safe='')},{lat},{lon}"

    hours = {}
    days = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일", "공휴일"]

    for number, day in enumerate(days, start=1):
        hours[day] = {
            "start": items.findtext(f"dutyTime{number}s"),
            "end": items.findtext(f"dutyTime{number}c"),
        }

    return ok({
        "id": items.findtext("hpid"),
        "name": items.findtext("dutyName"),
        "address": items.findtext("dutyAddr"),
        "hours": hours,
        "direction_url" : direction_url
    })



# ── ④ 등록: 이름으로 찾는 Tool 목록 ──────────────────────────
#모델한테 넘길 설명, 모델은 이것만 읽음! 
TOOLS = {t.name: t for t in [
    Tool(name="read_image",
         description="검진결과지 이미지 파일을 받아 검사 수치와 체크된 판정을 읽을 때 사용한다.",
         parameters={"type": "object",
                     "properties": {"image_path": {"type": "string"}},
                                                               "required": ["image_path"]},
         func=read_image),

    Tool(name="search_hospitals",
         description="진료과 병원을 정하지 못했다면 , 지역과 병력 기반으로 나온 진료과를 기반으로 필요한 병원을 추천한다." 
,
          parameters={"type": "object",
                     "properties": {"region": {"type": "string"}, "department":{"type" : "string"} , "page_no":{"type": "integer", "minimum": 1, "default":1}},
                     "required": ["region", "department"]
                      },
        func=search_hospitals),

    Tool(name="get_hospital_detail",
         description="병원ID로 해당 병원의 요일별 진료 시간, 주소, 길찾기 링크를 조회합니다." 
         "사용자의 방문 가능 시간을 기반으로 사용자 일정과 비교할 병원의 요일별 진료 시간을 조회합니다."
         ,
         parameters={"type" : "object", 
                     "properties": {"id" : {"type" : "string"}},
                     "required": ["id"]
                     },
         func=get_hospital_detail),
       
]}



def catalog():
    """Agent 에게 넘기는 것: 이름 · 설명 · 입력 규격뿐"""
    return [{"name": t.name, "description": t.description,
             "parameters": t.parameters} for t in TOOLS.values()]

if __name__ == "__main__":
    for c in catalog():                           # 등록한 목록 — Tool 정의서와 대조한다
        print("[Tool]", c["name"], "—", c["description"])
    tool = TOOLS["read_image"]

    def trial(name, **args):
        r = tool.run(**args)
        print(f"{r['ok']!s:<5} {name} →", r["data"] if r["ok"] else r["error"]["code"])


    sample = Path(__file__).resolve().parent / "data" / "senior-exam.png"

    trial("정상", image_path=str(sample))               
    key = os.environ.pop(KEY_NAME, "")          
    trial("키 없음", image_path =str(sample))
    os.environ[KEY_NAME] = key
    trial("결과 없음", image_path=str(sample.with_name("없는사진.png")))    

