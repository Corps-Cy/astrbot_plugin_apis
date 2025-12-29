import asyncio
import traceback
from collections import defaultdict

import aiohttp
from bs4 import BeautifulSoup

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from .api_manager import APIManager
from .utils import dict_to_string, extract_urls, get_nested_value, parse_api_keys


class RequestManager:
    headers = {
        # 核心防盗链
        "Referer": "https://www.meilishuo.com",
        # 浏览器 UA（Chrome 122 Win10 64bit）
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        # 接受类型（图片/网页通吃）
        "Accept": ("image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"),
        # 接受编码（节省流量）
        "Accept-Encoding": "gzip, deflate, br",
        # 接受语言
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        # 缓存控制
        "Cache-Control": "no-cache",
        # 长连接
        "Connection": "keep-alive",
        # 可选：模拟浏览器自动升级不安全请求
        "Upgrade-Insecure-Requests": "1",
        # 可选：防追踪头，部分 CDN 会参考
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "same-site",
    }

    def __init__(self, config: AstrBotConfig, api_manager: APIManager) -> None:
        self.session = aiohttp.ClientSession()
        # api密钥字典
        self.api_key_dict = parse_api_keys(config.get("api_keys", []).copy())
        self.api_sites = list(self.api_key_dict.keys())
        self.api = api_manager
        # 打印解析的API密钥信息
        if self.api_key_dict:
            logger.info(f"已解析 {len(self.api_key_dict)} 个API密钥配置")

    async def request(
        self, urls: list[str], params: dict | None = None, test_mode: bool = False
    ) -> bytes | str | dict | None:
        last_exc = None
        for url in urls:
            try:
                # 检查是否需要添加API密钥到请求参数
                request_params = dict(params) if params else {}
                base_url = self.api.extract_base_url(url)
                
                # 匹配时忽略协议差异（http/https）
                api_key = None
                if base_url in self.api_key_dict:
                    api_key = self.api_key_dict[base_url]
                else:
                    # 尝试切换协议匹配
                    if base_url.startswith("http://"):
                        https_url = base_url.replace("http://", "https://")
                        if https_url in self.api_key_dict:
                            api_key = self.api_key_dict[https_url]
                    elif base_url.startswith("https://"):
                        http_url = base_url.replace("https://", "http://")
                        if http_url in self.api_key_dict:
                            api_key = self.api_key_dict[http_url]
                
                # 如果找到密钥且params中还没有ckey，则添加
                if api_key and "ckey" not in request_params:
                    request_params["ckey"] = api_key
                
                # 打印请求参数
                logger.info(f"[请求] URL: {url}")
                logger.info(f"[请求] 参数: {request_params}")
                
                async with self.session.get(
                    url=url, headers=self.headers, params=request_params, timeout=30
                ) as resp:
                    resp.raise_for_status()
                    if test_mode:
                        return
                    ct = resp.headers.get("Content-Type", "").lower()
                    
                    # 根据响应类型获取数据并打印
                    if "application/json" in ct:
                        data = await resp.json()
                        logger.info(f"[响应] 状态码: {resp.status}, 类型: JSON, 数据: {data}")
                        return data
                    if "text/" in ct:
                        data = (await resp.text()).strip()
                        # 限制文本长度，避免日志过长
                        data_preview = data[:500] + "..." if len(data) > 500 else data
                        logger.info(f"[响应] 状态码: {resp.status}, 类型: TEXT, 数据: {data_preview}")
                        return data
                    data = await resp.read()
                    logger.info(f"[响应] 状态码: {resp.status}, 类型: BINARY, 数据长度: {len(data)} bytes")
                    return data
            except Exception as e:
                last_exc = e
                # 打印详细的异常信息
                error_detail = traceback.format_exc()
                logger.error(f"[请求失败] URL: {url}, 参数: {request_params}, 错误: {e}")
                logger.error(f"[请求失败] 详细错误信息:\n{error_detail}")
        if last_exc:
            raise last_exc

    async def get_data(
        self,
        urls: list[str],
        params: dict | None = None,
        api_type: str = "",
        target: str = "",
    ) -> tuple[str | None, bytes | None]:
        """对外接口，获取数据"""
        data = await self.request(urls, params)

        # data为URL时，下载数据
        if isinstance(data, str) and api_type != "text":
            if new_urls := extract_urls(data):
                downloaded = await self.request(new_urls)
                if isinstance(downloaded, bytes):
                    data = downloaded
                else:
                    raise RuntimeError(f"下载数据失败: {new_urls}")  # 抛异常给外部

        # data为字典时，解析字典
        if isinstance(data, dict) and target:
            nested_value = get_nested_value(data, target)
            if isinstance(nested_value, dict):
                data = dict_to_string(nested_value)
            else:
                data = nested_value

        # data为HTML字符串时，解析HTML
        if isinstance(data, str) and data.strip().startswith("<!DOCTYPE html>"):
            soup = BeautifulSoup(data, "html.parser")
            # 提取HTML中的文本内容
            data = soup.get_text(strip=True)

        text = data if isinstance(data, str) else None
        byte = data if isinstance(data, bytes) else None

        return text, byte

    async def batch_test_apis(self) -> tuple[list[str], list[str]]:
        """
        将每个 URL 都作为独立测试项按站点分组；每轮从每个站点 pop 一个 URL 并发测试，直到所有 URL 测完。
        返回 (abled_api_list, disabled_api_list)
        """
        # 1) 展平每个 API 的所有 URL -> 按 site 分组
        site_to_entries = defaultdict(list)  # site -> list of entries
        for api_name, api_data in self.api.apis.items():
            url = api_data["url"]
            urls = [url] if isinstance(url, str) else url
            for u in urls:
                site = self.api.extract_base_url(u)
                site_to_entries[site].append(
                    {
                        "api_name": api_name,
                        "url": u,
                        "params": api_data.get("params", {}),
                    }
                )

        # 2) 记录每个 API 是否已成功（任一 URL 成功即为成功）
        api_succeeded = dict.fromkeys(self.api.apis.keys(), False)

        # 3) 按轮次从每个站点各取一个 URL 并发测试，直到所有站点的 entry 列表空
        while any(site_to_entries.values()):
            batch = []
            for site, entries in list(site_to_entries.items()):
                while entries:
                    batch.append(entries.pop(0))
                    break

            if not batch:
                break  # 没有需要测试的 entry 了

            # 并发测试这一轮的所有 URL
            tasks = [self.request([e["url"]], e["params"], True) for e in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 处理结果：任何非 Exception 的返回都视为成功
            for entry, res in zip(batch, results):
                if isinstance(res, Exception):
                    pass
                else:
                    api_succeeded[entry["api_name"]] = True

        # 4) 汇总
        abled = [k for k, v in api_succeeded.items() if v]
        disabled = [k for k, v in api_succeeded.items() if not v]
        return abled, disabled

    async def terminate(self):
        """关闭会话，断开连接"""
        await self.session.close()
