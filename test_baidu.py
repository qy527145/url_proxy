import json
import os
import time

import requests


def to_timestamp(t: str = None):
    return time.strptime(t, '%Y-%m-%d %H:%M:%S') if t else int(time.time())


class BaiduPanToken:
    def __init__(self, bduss=None, storage_path='./token.json'):
        self.bduss = bduss
        self.refresh_token = None
        self.access_token = None
        self.expires = None
        self.storage_path = storage_path
        self.load_token()

    def get_token(self):
        if not self.is_valid():
            self.refresh()
            self.save_token()
        return self.access_token

    def is_valid(self):
        now = to_timestamp()
        return (not not self.access_token) and self.expires and now < self.expires

    def load_token(self):
        if not self.storage_path:
            return
        if os.path.exists(self.storage_path):
            with open(self.storage_path, 'r') as file:
                token_json = json.load(file)
                if not self.bduss:
                    self.bduss = token_json.get('bduss')
                if not self.refresh_token:
                    self.refresh_token = token_json.get('refresh_token')
                if not self.access_token:
                    self.access_token = token_json.get('access_token')
                if not self.expires:
                    self.expires = token_json.get('expires')

    def save_token(self):
        if not self.storage_path:
            return
        token_json = {}
        if os.path.exists(self.storage_path):
            with open(self.storage_path, 'r') as f1:
                token_json = json.load(f1)
        token_json.update({
            'bduss': self.bduss,
            'refresh_token': self.refresh_token,
            'access_token': self.access_token,
            'expires': self.expires
        })
        with open(self.storage_path, 'w') as f2:
            json.dump(token_json, f2, indent=2)

    def refresh(self):
        if not self.bduss:
            raise Exception('BDUSS未设置')
        if not self.refresh_token:
            client_id = 'iYCeC9g08h5vuP9UqvPHKKSVrKFXGa1v'
            client_secret = 'jXiFMOPVPCWlO2M5CwWQzffpNPaGTRBG'
            url = 'https://openapi.baidu.com/oauth/2.0/device/code' \
                  '?response_type=device_code' \
                  '&client_id=iYCeC9g08h5vuP9UqvPHKKSVrKFXGa1v' \
                  '&scope=basic,netdisk'
            data = requests.get(url, headers={
                'User-Agent': 'pan.baidu.com'
            }).json()
            device_code = data['device_code']
            requests.get(
                f'https://openapi.baidu.com/device?code={data["user_code"]}&display=page&redirect_uri=&force_login=',
                cookies={
                    'BDUSS': self.bduss
                })
            now = to_timestamp()
            resp = requests.get(
                f'https://openapi.baidu.com/oauth/2.0/token?grant_type=device_token&code={device_code}&client_id={client_id}&client_secret={client_secret}')
            resp.raise_for_status()
            token_info = resp.json()
            if 'error_description' in token_info:
                raise Exception(token_info['error_description'])
            self.refresh_token = token_info.get('refresh_token')
            self.access_token = token_info.get('access_token')
            self.expires = now + token_info.get('expires_in')
        else:
            client_id = 'iYCeC9g08h5vuP9UqvPHKKSVrKFXGa1v'
            client_secret = 'jXiFMOPVPCWlO2M5CwWQzffpNPaGTRBG'
            now = to_timestamp()
            token_info = requests.get('https://openapi.baidu.com/oauth/2.0/token', params={
                'grant_type': 'refresh_token',
                'refresh_token': self.refresh_token,
                'client_id': client_id,
                'client_secret': client_secret,
            }).json()
            self.refresh_token = token_info.get('refresh_token')
            self.access_token = token_info.get('access_token')
            self.expires = now + token_info.get('expires_in')


class File:
    def __init__(self, auth, file_info):
        self._auth = auth
        self._ua = 'netdisk'
        for k, v in file_info.items():
            setattr(self, k, v)

    def __str__(self):
        return self.server_filename

    def list(self, dir=None):
        if dir is None:
            dir = self.path
        if self.isdir != 1:
            raise Exception('文件夹才有下一级')
        resp = requests.get('https://pan.baidu.com/rest/2.0/xpan/file', params={
            'method': 'list',
            'dir': dir,
            'access_token': self._auth.get_token(),
        })
        resp.raise_for_status()
        return [File(self._auth, i) for i in resp.json()['list']]

    def detail(self):
        if self.isdir == 1:
            raise Exception('文件夹无法查看详情')
        resp = requests.get('https://pan.baidu.com/rest/2.0/xpan/multimedia', params={
            'method': 'filemetas',
            'access_token': self._auth.get_token(),
            'fsids': f'[{self.fs_id}]',
            'dlink': '1',
        }, headers={
            'User-Agent': self._ua,
        })
        resp.raise_for_status()
        return resp.json()['list'][0]

    def get_download_url(self):
        # Open官方接口，非会员限速
        if self.isdir == 1:
            raise Exception('不支持文件夹下载')
        resp = requests.get('https://pan.baidu.com/rest/2.0/xpan/multimedia', params={
            'method': 'filemetas',
            'fsids': f'[{self.fs_id}]',
            'dlink': '1',
            'access_token': self._auth.get_token(),
        })
        resp.raise_for_status()
        return f'{resp.json()['list'][0]['dlink']}&access_token={self._auth.get_token()}'

    def get_download_url1(self):
        # 隐藏的api, 支持多线程且不限制分片, 但是不稳定
        if self.isdir == 1:
            raise Exception('不支持文件夹下载')
        resp = requests.get('https://pan.baidu.com/api/filemetas', params={
            'target': f'["{self.path}"]',
            'dlink': 1,
            'web': 5,
            'origin': 'dlna',
            'access_token': self._auth.get_token(),
        }, headers={
            'User-Agent': self._ua,
        })
        resp.raise_for_status()
        return f"{resp.json()['info'][0]['dlink']}&access_token={self._auth.get_token()}"

    def cmd(self):
        download_url = self.get_download_url1()
        command = f'aria2c -x16 "{download_url}" --header="User-Agent: {self._ua}"'
        print(command)
        return command

    def play_proxy(self):
        from core import URLProxy
        URLProxy(urls=self.get_download_url1(), trunk='8M', split='1M', conns=2, headers={
            'User-Agent': self._ua,
        }).proxy()

    def download(self):
        from core import URLProxy
        URLProxy(urls=self.get_download_url1(), trunk='8M', split='1M', conns=2, headers={
            'User-Agent': self._ua,
        }).download()


class BaiduPan:
    def __init__(self, auth: BaiduPanToken):
        self.auth = auth
        self.root = File(auth, {'isdir': 1, 'path': '/', 'server_filename': 'root'})


if __name__ == '__main__':
    BDUSS = '<YOUR_BDUSS>'
    auth = BaiduPanToken(BDUSS)
    obj = BaiduPan(auth)
    obj.root.list()[-1].download()
    # obj.root.list()[-1].play_proxy()
