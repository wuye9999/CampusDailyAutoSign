__all__ = ['ActionBase']

import base64
import json
import uuid
from urllib.parse import urlparse

import requests
from requests.exceptions import *
from pyDes import des, CBC, PAD_PKCS5

from BusinessCentralLayer.sentinel.noticer import send_email
from config import ADDRESS, LONGITUDE, LATITUDE, QnA, ABNORMAL_REASON, DEBUG, logger, TIME_ZONE_CN, SECRET_NAME

LOGGING_API = "http://www.zimo.wiki:8080/wisedu-unified-login-api-v1.0/api/login"


# 交互仓库
class ActionBase(object):
    """交互仓库"""

    def __init__(self, silence=False, ):
        # 静默模式
        self.silence = silence
        self.login_url = ''
        self.school_token = None
        # 错误日志
        self.error_msg = ''
        self.user_info = None
        self.apis = dict()

    # 获取今日校园api
    def get_campus_daily_apis(self, user):
        apis = {}
        schools = requests.get(url='https://www.cpdaily.com/v6/config/guest/tenant/list').json()['data']
        flag = True
        if self.school_token:
            user['school'] = self.school_token

        for one in schools:
            if one['name'] == user['school']:
                if one['joinType'] == 'NONE':
                    logger.info(user['school'] + ' 未加入今日校园')
                    return False
                flag = False
                params = {'ids': one['id']}
                res = requests.get(url='https://www.cpdaily.com/v6/config/guest/tenant/info', params=params, )
                data = res.json()['data'][0]
                joinType = data['joinType']
                idsUrl = data['idsUrl']
                target_url = data['ampUrl'] if 'campusphere' in data['ampUrl'] or 'cpdaily' in data['ampUrl'] else data[
                    'ampUrl2']
                parse = urlparse(target_url)
                host = parse.netloc
                res = requests.get(parse.scheme + '://' + host)
                parse = urlparse(res.url)
                apis[
                    'login-url'] = idsUrl + '/login?service=' + parse.scheme + r"%3A%2F%2F" + host + r'%2Fportal%2Flogin'
                apis['host'] = host
                break
        if flag:
            logger.error(user['school'] + ' 未找到该院校信息，请检查是否是学校全称错误')
            return False
        # self.log(apis)
        return apis

    # 登陆并获取session
    def get_session(self, user, apis, retry=0, delay=10, max_retry_num=100):
        if retry >= max_retry_num:
            return False
        params = {
            'login_url': apis['login-url'],
            'needcaptcha_url': '',
            'captcha_url': '',
            'username': user['username'],
            'password': user['password']
        }
        cookies = dict()
        try:
            # fixme:统一认证接口抽风，原因未知，故当前版本不启用proxy方案
            res = requests.post(url=LOGGING_API, data=params)
            res.raise_for_status()
            if res.status_code == 200:
                cookieStr = str(res.json()['cookies'])
                if cookieStr == 'None':
                    if "网页中没有找到casLoginForm" in res.json()['msg']:
                        return None
                    else:
                        return False

                # 解析cookie
                for line in cookieStr.split(';'):
                    name, value = line.strip().split('=', 1)
                    cookies[name] = value
                session = requests.session()
                session.cookies = requests.utils.cookiejar_from_dict(cookies, cookiejar=None, overwrite=True)
                return session
        except json.decoder.JSONDecodeError or ProxyError:
            logger.warning("目标或存在鉴权行为，请关闭本地网络代理")
        except RequestException:
            retry += 1
            self.get_session(user, apis, retry)

    # 获取最新未签到任务
    def get_unsigned_tasks(self, session, apis=None):
        if not apis:
            apis = self.apis
        headers = {
            # 'Accept': 'application/json, text/plain, */*',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/83.0.4103.97 Safari/537.36',
            'content-type': 'application/json',
            'Accept-Encoding': 'gzip,deflate',
            'Accept-Language': 'zh-CN,en-US;q=0.8',
            'Content-Type': 'application/json;charset=UTF-8'
        }
        # 第一次请求每日签到任务接口，主要是为了获取MOD_AUTH_CAS
        res = session.post(
            url='https://{host}/wec-counselor-sign-apps/stu/sign/getStuSignInfosInOneDay'.format(host=apis['host']),
            headers=headers, data=json.dumps({}))
        # fixme:DEBUG module-- response status code :404
        try:
            latestTask = res.json()['datas']['unSignedTasks'][0]
            return {
                'signInstanceWid': latestTask['signInstanceWid'],
                'signWid': latestTask['signWid']
            }
        except json.decoder.JSONDecodeError:
            msg = f'the response of queryClass is None! ({res.status_code})|| Base on function(get_unsigned_tasks)'
            if res.status_code != 200:
                s_msg = f"{msg}\n可能原因为：负责捕获MOD_AUTH_CAS任务的分布式节点异常/接口改动/资源请求方式改动"
                # self.send_email(s_msg, to=self.user_info['email'], headers='今日校园提醒您 -> 体温签到')
                return False
        except IndexError:
            return False

    # 获取签到任务详情
    def get_detail_task(self, session, params, apis=None):
        if not apis:
            apis = self.apis
        headers = {
            'Accept': 'application/json, text/plain, */*',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/83.0.4103.97 Safari/537.36',
            'content-type': 'application/json',
            'Accept-Encoding': 'gzip,deflate',
            'Accept-Language': 'zh-CN,en-US;q=0.8',
            'Content-Type': 'application/json;charset=UTF-8'
        }
        res = session.post(
            url='https://{host}/wec-counselor-sign-apps/stu/sign/detailSignInstance'.format(host=apis['host']),
            headers=headers, data=json.dumps(params), verify=not DEBUG)
        data = res.json()['datas']

        return data

    # 填充表单
    def fill_form(self, task: dict, session, user, apis, ):
        user = user['user']
        form = {
            'signPhotoUrl': '',
            'longitude': LONGITUDE,
            'latitude': LATITUDE,
            'position': ADDRESS,
            'abnormalReason': ABNORMAL_REASON,
        }
        if task['isNeedExtra'] == 1:
            extraFields = task['extraField']
            questions = list(QnA.keys())
            extraFieldItemValues = []
            for i in range(0, len(extraFields)):
                question = questions[i]
                extraField = extraFields[i]
                extraFieldItems = extraField['extraFieldItems']
                for extraFieldItem in extraFieldItems:
                    if extraFieldItem['content'] == question:
                        extraFieldItemValue = {'extraFieldItemValue': QnA[question],
                                               'extraFieldItemWid': extraFieldItem['wid']}
                        # # 其他，额外文本
                        # if extraFieldItem['isOtherItems'] == 1:
                        #     extraFieldItemValue = {'extraFieldItemValue': question['other'],
                        #                            'extraFieldItemWid': extraFieldItem['wid']}
                        extraFieldItemValues.append(extraFieldItemValue)
            # log(extraFieldItemValues)
            # 处理带附加选项的签到
            form['extraFieldItems'] = extraFieldItemValues
        form['signInstanceWid'] = task['signInstanceWid']
        form['isMalposition'] = task['isMalposition']
        return form

    @staticmethod
    def upload_picture(session, image, apis):
        import oss2
        url = 'https://{host}/wec-counselor-sign-apps/stu/sign/getStsAccess'.format(host=apis['host'])
        res = session.post(url=url, headers={'content-type': 'application/json'}, data=json.dumps({}), verify=not DEBUG)
        data = res.json().get('datas')
        fileName = data.get('fileName')
        accessKeyId = data.get('accessKeyId')
        accessSecret = data.get('accessKeySecret')
        securityToken = data.get('securityToken')
        endPoint = data.get('endPoint')
        bucket = data.get('bucket')
        bucket = oss2.Bucket(oss2.Auth(access_key_id=accessKeyId, access_key_secret=accessSecret), endPoint, bucket)
        with open(image, "rb") as f:
            data = f.read()
        bucket.put_object(key=fileName, headers={'x-oss-security-token_msg': securityToken}, data=data)
        res = bucket.sign_url('PUT', fileName, 60)
        # log(res)
        return fileName

    # 获取图片上传位置
    @staticmethod
    def get_picture_url(session, fileName, apis):
        url = 'https://{host}/wec-counselor-sign-apps/stu/sign/previewAttachment'.format(host=apis['host'])
        data = {
            'ossKey': fileName
        }
        res = session.post(url=url, headers={'content-type': 'application/json'}, data=json.dumps(data),
                           verify=not DEBUG)
        photoUrl = res.json().get('datas')
        return photoUrl

    # DES加密
    @staticmethod
    def DESEncrypt(s, key='ST83=@XV'):
        key = key
        iv = b"\x01\x02\x03\x04\x05\x06\x07\x08"
        k = des(key, CBC, iv, pad=None, padmode=PAD_PKCS5)
        encrypt_str = k.encrypt(s)
        return base64.b64encode(encrypt_str).decode()

    # 提交签到任务
    def submitForm(self, session, user, form, apis=None):
        if not apis:
            apis = self.apis
        # campus daily Extension
        extension = {
            "lon": LONGITUDE,
            "model": "OPPO R11 Plus",
            "appVersion": "8.1.14",
            "systemVersion": "4.4.4",
            "userId": user['username'],
            "systemName": "android",
            "lat": LATITUDE,
            "deviceId": str(uuid.uuid1())
        }

        headers = {
            # 'tenantId': '1019318364515869',
            'User-Agent': 'Mozilla/5.0 (Linux; Android 4.4.4; OPPO R11 Plus Build/KTU84P) AppleWebKit/537.36 (KHTML, '
                          'like Gecko) Version/4.0 Chrome/33.0.0.0 Safari/537.36 okhttp/3.12.4',
            'CpdailyStandAlone': '0',
            'extension': '1',
            'Cpdaily-Extension': self.DESEncrypt(json.dumps(extension)),
            'Content-Type': 'application/json; charset=utf-8',
            'Accept-Encoding': 'gzip',
            # 'Host': 'swu.cpdaily.com',
            'Connection': 'Keep-Alive'
        }
        res = session.post(
            url='https://{host}/wec-counselor-sign-apps/stu/sign/submitSign'.format(host=apis['host']),
            headers=headers, data=json.dumps(form), verify=not DEBUG)
        try:
            return res.json()['message']
        except json.decoder.JSONDecodeError as e:
            logger.debug(e)

    @staticmethod
    def send_email(msg, to, headers=None):
        from config import ENABLE_EMAIL
        from datetime import datetime
        if ENABLE_EMAIL:
            if msg == 'error':
                response = send_email(
                    msg=f'[{str(datetime.now(TIME_ZONE_CN)).split(".")[0]}]自动签到失败<From.{SECRET_NAME}>',
                    to=to,
                    headers='今日校园提醒您 -> ♂体温签到'
                )
            elif msg == 'success':
                response = send_email(
                    msg=f'[{str(datetime.now(TIME_ZONE_CN)).split(".")[0]}]自动签到成功<From.{SECRET_NAME}>',
                    to=to,
                    headers=None
                )
            else:
                response = send_email(msg, to, headers)
            return response
