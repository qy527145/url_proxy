import concurrent
import re
from abc import abstractmethod
# from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from multiprocessing import Pipe
from multiprocessing import Process

# import requests
import httpx
import uvicorn
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import StreamingResponse
from humanfriendly import parse_size
from tqdm import tqdm


# 多源地址、多线程、播放代理

# todo
# 负载均衡、代理池、边下边播

class Source:
    @abstractmethod
    def get(self, begin, end):
        pass

    @abstractmethod
    def info(self):
        pass


class URLSource(Source):
    def __init__(self, url, headers, cookies, conns=8):
        self.url = url
        self.headers = headers
        self.cookies = cookies
        # request 客户端
        # self.session = requests.Session()
        # from requests.packages.urllib3.util.retry import Retry
        # retries = Retry(total=5, backoff_factor=0.1, status_forcelist=[500, 502, 503, 504])
        # adapter = HTTPAdapter(pool_connections=100, pool_maxsize=10, max_retries=retries)
        # self.session.mount('http://', adapter)
        # self.session.mount('https://', adapter)
        # httpx 客户端
        self.session = httpx.Client(
            limits=httpx.Limits(max_connections=conns, max_keepalive_connections=conns)
        )

        self.session.headers.update(self.headers)
        self.session.cookies.update(self.cookies)

    def get(self, begin, end):
        return self.session.get(
            self.url,
            headers={'Range': f'bytes={begin}-{end}'}
        ).content, begin, end

    def info(self):
        resp = self.session.get(self.url, headers={'Range': 'bytes=0-0'})
        # 获取响应类型
        content_type = resp.headers['Content-Type']
        # 获取文件名称
        file_name = resp.headers['Content-Disposition'].split('"')[-2]
        try:
            file_name = file_name.encode('iso-8859-1').decode()
        except UnicodeEncodeError:
            pass
        # 获取文件大小
        length = int(resp.headers['Content-Range'].split('/')[-1])
        return content_type, file_name, length


class Selector:
    def __init__(self, targets):
        self.targets = targets

        # 轮询
        def loop():
            while True:
                for target in targets:
                    yield target

        self.gen = loop()

    def select(self):
        return next(self.gen)


class SourceGroup(Source):
    def __init__(self, selector: Selector):
        self.selector = selector

    def get(self, begin, end):
        return self.selector.select().get(begin, end)

    def info(self):
        return self.selector.select().info()


class Spliter:
    def __init__(self, *, begin=None, end=None, length=None):
        if begin is not None and end is not None:
            self.begin = begin
            self.length = end - begin + 1
        elif length:
            self.begin = 0
            self.length = length
        else:
            raise Exception('切片器参数不全')

    def iter(self, *, split: int | str = '5M', begin=None, end=None):
        if isinstance(split, str):
            split = parse_size(split, True)
        if not begin:
            begin = self.begin
        if not end:
            end = self.begin + self.length - 1

        def gen():
            left, right = begin, begin + split - 1

            while right <= end:
                yield left, right
                left, right = right + 1, right + split
            if left <= end:
                yield left, end

        return gen()

    def sub_split(self, trunk: int | str = '10M'):
        if isinstance(trunk, str):
            trunk = parse_size(trunk, True)

        def gen():
            for begin, end in self.iter(split=trunk):
                yield Spliter(begin=begin, end=end)

        return gen()


def write_task(pipe, file_name):
    msg = pipe.recv()
    with open(file_name, "rb+") as f:
        while msg is not None:
            data, index = msg
            f.seek(index)
            f.write(data)
            msg = pipe.recv()
    pipe.send(None)


class URLProxy:
    def __init__(
            self,
            urls,
            headers=None,
            cookies=None,
            trunk: str | int = '8M',
            split: str | int = '1M',
            conns=8
    ):
        if cookies is None:
            cookies = dict()
        if headers is None:
            headers = dict()
        if isinstance(trunk, str):
            trunk = parse_size(trunk, True)
        self.trunk = trunk
        if isinstance(split, str):
            split = parse_size(split, True)
        self.split = split
        if isinstance(urls, list):
            self.source = SourceGroup(Selector([URLSource(url, headers, cookies, conns) for url in urls]))
            self.workers = conns * len(urls)
        else:
            self.source = URLSource(urls, headers, cookies, conns)
            self.workers = conns
        self.content_type, self.file_name, self.length = self.source.info()

    def stream(self, *, begin=None, end=None, split=None):
        if not begin:
            begin = 0
        if not end:
            end = self.length - 1
        if not split:
            split = self.split
        spliter = Spliter(begin=begin, end=end)
        # executor = ProcessPoolExecutor(max_workers=self.workers)
        executor = ThreadPoolExecutor(max_workers=self.workers)

        for future in concurrent.futures.as_completed(
                [executor.submit(self.source.get, b, e) for b, e in spliter.iter(split=split)]
        ):
            content, b, e = future.result()
            yield content, b, e

    def sorted_stream(self, *, begin=None, end=None, trunk=None, split=None):
        if not begin:
            begin = 0
        if not end:
            end = self.length - 1
        if not trunk:
            trunk = self.trunk
        if not split:
            split = self.split
        spliter = Spliter(begin=begin, end=end)
        for l, r in spliter.iter(split=trunk):  # noqa: E741
            content = BytesIO()
            for data, b, e in self.stream(begin=l, end=r, split=split):
                content.seek(b - l)
                content.write(data)
            yield content.getvalue()

    def download(self):
        print('开始下载', self.file_name, '...')
        with open(self.file_name, "wb"):
            pass

        main, sub = Pipe()

        sub_task = Process(target=write_task, args=(sub, self.file_name))
        sub_task.start()
        tqdm_obj = tqdm(total=self.length + 100, unit_scale=True)
        for content, b, e in self.stream(split=self.split):
            main.send((content, b))
            tqdm_obj.update(len(content))
        main.send(None)
        main.recv()
        tqdm_obj.update(100)
        print('下载完成')

    def proxy(self, host='127.0.0.1', port=9999):

        app = FastAPI()

        @app.get("/")
        async def play(request: Request):
            range_str = request.headers.get("Range")
            size = self.length
            if not range_str:
                end = min(self.split, size) - 1
                return StreamingResponse(self.sorted_stream(begin=0, end=end), headers={
                    'Content-Range': f'bytes 0-{end}/{size}',
                })
            match = re.compile(r'bytes=(\d+)-(\d*)').match(range_str)
            begin, end = match.groups()
            begin = int(begin) if begin else 0
            # end = int(end) if end else size - 1
            # 调整缓冲范围
            # begin = max(0, begin - self.split)
            end = min(begin + self.trunk, size) - 1
            try:
                return StreamingResponse(self.sorted_stream(begin=begin, end=end), headers={
                    'Content-Range': f'bytes {begin}-{end}/{size}',
                })
            except Exception:
                raise HTTPException(status_code=404)

        uvicorn.run(app, host=host, port=port)


if __name__ == '__main__':
    # urls = 'your url link'
    # obj = URLProxy(urls=urls, trunk='8M', split='1M', conns=2, headers=headers)
    # obj.download()
    # obj.proxy()
    pass
