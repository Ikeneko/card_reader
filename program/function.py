from bs4 import BeautifulSoup
import requests

# --- 定数設定 ---
URLS = {
    'bachelor': 'https://mars10.mars.kanazawa-it.ac.jp/seiseki/ed3_g',
    'master': 'https://mars10.mars.kanazawa-it.ac.jp/seiseki/sensyu_g'
}

TRX_CONFIG = {
    'bachelor': {
        'menu': {"_TRXID": "REDTGN0201", "_INPAGEID": "DSTUDENTMENU"},
        'detail': {"_TRXID": "REDTGN0204", "_INPAGEID": "DEDTGN0201"},
        'back': {"_TRXID": "REDTGN0201B", "_INPAGEID": "DEDTGN0204"},
        'inputmenu': {"_TRXID": "REDTGN0202", "_INPAGEID": "DEDTGN0201"},
        'input': {"_TRXID": "REDTGN0203", "_INPAGEID": "DEDTGN0202"},
        'editmenu': {"_TRXID": "REDTGN0205", "_INPAGEID": "DEDTGN0204"},
        'edit': {"_TRXID": "REDTGN0206", "_INPAGEID": "DEDTGN0205"},
        'delete': {"_TRXID": "REDTGN0207", "_INPAGEID": "DEDTGN0204"}
    },
    'master': {
        'menu': {"_TRXID": "REDTG0201", "_INPAGEID": "DSTUDENTMENU"},
        'detail': {"_TRXID": "REDTG1204", "_INPAGEID": "DEDTG0201"},
        'back': {"_TRXID": "REDTG0201B", "_INPAGEID": "DEDTG1204"},
        'inputmenu': {"_TRXID": "REDTG1202", "_INPAGEID": "DEDTG0201"},
        'input': {"_TRXID": "REDTG1203", "_INPAGEID": "DEDTG1202"},
        'editmenu': {"_TRXID": "REDTG1205", "_INPAGEID": "DEDTG1204"},
        'edit': {"_TRXID": "REDTG1206", "_INPAGEID": "DEDTG1205"},
        'delete': {"_TRXID": "REDTG1207", "_INPAGEID": "DEDTG1204"}
    }
}

# --- _CONTCHECK を抽出 ---
def _contcheck(response):
    soup = BeautifulSoup(response.text, 'html.parser')
    contcheck_tag = soup.find('input', {'name': '_CONTCHECK'})
    return contcheck_tag['value'] if contcheck_tag else None

# --- ログイン ---
def login_payload(web_session, user_id, password, select_course):
    login_data = {
        "_TRXID": "LOGIN",
        "uid": user_id,
        "pw": password
    }
    url = URLS[select_course]
    response = web_session.post(url, data=login_data)
    return web_session, response

# --- 「活動記録機能」をクリックする操作を模倣 ---
def menu_payload(web_session, response, select_course):
    contcheck_value = _contcheck(response)
    payload = TRX_CONFIG[select_course]['menu'].copy()
    payload["_CONTCHECK"] = contcheck_value
    payload["timestamp"] = "null"
    url = URLS[select_course]
    response = web_session.post(url, data=payload)
    return web_session, response

# --- 各活動詳細を取得 ---
def detail_payload(web_session, response, select_course, seqno):
    contcheck_value = _contcheck(response)
    payload = TRX_CONFIG[select_course]['detail'].copy()
    payload["_CONTCHECK"] = contcheck_value
    payload["seqno"] = seqno
    url = URLS[select_course]
    response = web_session.post(url, data=payload)
    return web_session, response

# --- 一覧ページへ戻る ---
def back_payload(web_session, response, select_course):
    contcheck_value = _contcheck(response)
    payload = TRX_CONFIG[select_course]['back'].copy()
    payload["_CONTCHECK"] = contcheck_value
    url = URLS[select_course]
    response = web_session.post(url, data=payload)
    return web_session, response

# --- 「活動記録の新規登録」をクリックする操作を模倣 ---
def inputmenu_payload(web_session, response, select_course):
    contcheck_value = _contcheck(response)
    payload = TRX_CONFIG[select_course]['inputmenu'].copy()
    payload["_CONTCHECK"] = contcheck_value
    url = URLS[select_course]
    response = web_session.post(url, data=payload)
    return web_session, response
    
# --- 新規データ登録 ---
def input_payload(web_session, response, select_course, input_data):
    contcheck_value = _contcheck(response)
    payload = TRX_CONFIG[select_course]['input'].copy()
    payload["_CONTCHECK"] = contcheck_value
    payload["syear"] = input_data["syear"]
    payload["smonth"] = input_data["smonth"]
    payload["sday"] = input_data["sday"]
    payload["eyear"] = input_data["eyear"]
    payload["emonth"] = input_data["emonth"]
    payload["eday"] = input_data["eday"]
    payload["k_jikan"] = input_data["k_jikan"]
    payload["k_naiyou"] = input_data["k_naiyou"].encode('shift_jis')
    url = URLS[select_course]
    response = web_session.post(url, data=payload)
    return web_session, response
    
# --- 編集する活動歴をクリックする操作を模倣 ---
def editmenu_payload(web_session, response, select_course, seqno):
    contcheck_value = _contcheck(response)
    payload = TRX_CONFIG[select_course]['editmenu'].copy()
    payload["_CONTCHECK"] = contcheck_value
    payload["seqno"] = seqno
    url = URLS[select_course]
    response = web_session.post(url, data=payload)
    return web_session, response

# --- 変更データ登録 ---
def edit_payload(web_session, response, select_course, seqno, edit_data):
    contcheck_value = _contcheck(response)
    payload = TRX_CONFIG[select_course]['edit'].copy()
    payload["_CONTCHECK"] = contcheck_value
    payload["seqno"] = seqno
    payload["syear"] = edit_data["syear"]
    payload["smonth"] = edit_data["smonth"]
    payload["sday"] = edit_data["sday"]
    payload["eyear"] = edit_data["eyear"]
    payload["emonth"] = edit_data["emonth"]
    payload["eday"] = edit_data["eday"]
    payload["k_jikan"] = edit_data["k_jikan"]
    payload["k_naiyou"] = edit_data["k_naiyou"].encode('shift_jis')
    url = URLS[select_course]
    response = web_session.post(url, data=payload)
    return web_session, response

# --- 削除する活動歴をクリックする操作を模倣 ---
def delete_payload(web_session, response, select_course, seqno):
    contcheck_value = _contcheck(response)
    payload = TRX_CONFIG[select_course]['delete'].copy()
    payload["_CONTCHECK"] = contcheck_value
    payload["seqno"] = seqno
    url = URLS[select_course]
    response = web_session.post(url, data=payload)
    return web_session, response

