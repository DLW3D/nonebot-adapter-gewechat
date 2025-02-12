import qrcode
import asyncio
import ujson as json

from typing import Any, Optional, List, Dict, Union
from typing_extensions import override
from pydantic import ValidationError

from nonebot import get_plugin_config
from nonebot.drivers import (
    URL,
    Driver,
    Request,
    Response,
    ASGIMixin,
    HTTPClientMixin,
    HTTPServerSetup,
    ASGIMixin
)

from nonebot.adapters import Adapter as BaseAdapter

from .bot import Bot
from .event import Event
from .config import Config
from .message import Message, MessageSegment
from .utils import log, resp_json
from .model import *
from .exception import ActionFailed, NetworkError


class Adapter(BaseAdapter):
    token: str = ""

    @override
    def __init__(self, driver: Driver, **kwargs: Any):
        super().__init__(driver, **kwargs)
        self.adapter_config = get_plugin_config(Config)
        self.on_ready(self.startup)

    async def startup(self) -> None:
        """定义启动时的操作，例如和平台建立连接"""
        await self.setup()

    async def _setup_http(self) -> None:
        if not isinstance(self.driver, ASGIMixin):
            raise RuntimeError(
                f"Current driver {self.config.driver} doesn't support ReverseDriver server!"
                f"{self.get_name()} Adapter need a ReverseDriver server driver to work."
            )
        
        http_setup = HTTPServerSetup(
            URL(self.adapter_config.gewechat_callback_path),
            method="POST",
            name="gewechat_callback",
            handle_func=self._handle_http
        )
        
        self.setup_http_server(http_setup)

    async def _setup_bot(self) -> None:
        if not isinstance(self.driver, HTTPClientMixin):
            raise RuntimeError(
                f"Current driver {self.config.driver} doesn't support ForwardDriver server!"
                f"{self.get_name()} Adapter need a ForwardDriver server driver to work."
            )
        
        connected: bool = False
        wxid = self.adapter_config.wxid
        appId = self.adapter_config.appid
        # 1. 获取token
        log("DEBUG", "获取token")
        await self.get_token()
        # 2. 获取二维码
        log("DEBUG", "获取二维码")
        data: dict = resp_json(await self._do_call_api(
            "/login/getLoginQrCode",
            appId=appId
        ))
        if data['ret'] != 200 and data['msg'] != '微信已登录，请勿重复登录。':
            log("ERROR", "获取二维码失败,请检查网络环境")
            raise NetworkError("获取二维码失败")
        elif data["ret"] == 200:
            appId: str = data['data']['appId']
            qr_code = qrcode.QRCode()
            qr_code.add_data(data['data']['qrData'])
            qr_code.print_ascii(invert=True)
            log("INFO", "请使用微信扫描二维码登录")
            captchCode: str = input("扫码后输入验证码(如果有): ")
        else:
            log("INFO", "微信已登录")
            appId = self.adapter_config.appid
            connected = True

        count: int = 0
        # 3. 轮询登录状态
        while connected is False:
            log("DEBUG", "轮询登录状态")
            data: dict = resp_json(await self._do_call_api(
                "/login/checkLogin",
                appId=appId,
                uuid=data['data']['uuid'],
                captchCode=captchCode
            ))
            count += 1
            if data['ret'] == 200:
                if data['data']['status'] == 2:
                    wxid = data['data']['loginInfo']['wxid']
                    log("INFO", "登录成功")
                    log("INFO", 
                        f"登录用户: {data['data']['loginInfo']['nickName']}\n" +
                        f"wxid: {wxid}\tappId: {appId}\n" +
                        "请妥善保管登录信息,频繁更换appId可能导致风控\n" +
                        "填写appId,wxid到配置文件中,重启即可使用")
                    break
            elif count > 15:
                log("ERROR", "登录失败,请重启后重新扫码登录")
                raise NetworkError("登录失败")
            else:
                asyncio.sleep(5)

        bot = Bot(
            self,
            wxid
        )
        self.bot_connect(bot)

        count = 0
        # 4. 设置回调地址
        log("DEBUG", "设置回调地址")
        while True:
            data = resp_json(await self._do_call_api(
                "/tools/setCallback",
                token=self.token,
                callbackUrl=self.adapter_config.gewechat_callback_url
            ))
            count += 1
            if data['ret'] == 200:
                break
            elif count > 15:
                log("ERROR", "设置回调地址超时,检查配置是否正确")
                raise NetworkError("设置回调地址失败")
            else:
                await asyncio.sleep(5)


    async def setup(self) -> None:
        await self._setup_http()
        asyncio.create_task(self._setup_bot())

    @classmethod
    @override
    def get_name(cls) -> str:
        return "gewechat"

    async def get_token(self) -> None:
        """获取token"""
        data: Dict = resp_json(await self._do_call_api("/tools/getTokenId"))
        if data['ret'] != 200:
            raise NetworkError("获取token失败: " + str(data))
        self.token = data['data']

    @override
    async def _call_api(self, bot: Bot, api: str, **data: Any) -> Response:
        re = await self._do_call_api(api, **data)
        if re.status_code != 200:
            raise ActionFailed(f"调用API失败: {re.status_code}")
        else:
            content = json.loads(re.content.decode("utf-8"))
            if content["ret"] != 200:
                raise ActionFailed(f"调用API失败: {str(content)}")
            else:
                return re

    async def _do_call_api(self, api: str, **data: Any) -> Response:
        log("DEBUG", f"Calling API <y>{api}</y>")
        api_url = self.adapter_config.gewechat_api_url + api.strip()
        headers = {
            "Content-Type": "application/json",
            "X-GEWE-TOKEN": self.token
        }
        request = Request(
            "POST",
            api_url,
            json=data,
            headers=headers
        )

        re = await self.driver.request(request)
        # 如果请求失败, 重新获取token并重试
        if re.status_code != 200:
            await self.get_token()
            log("DEBUG", f"Calling API <y>{api}</y>")
            api_url = self.adapter_config.gewechat_api_url + api
            headers = {
                "Content-Type": "application/json",
                "X-GEWE-TOKEN": self.token
            }
            request = Request(
                "POST",
                api_url,
                json=data,
                headers=headers
            )
            re = await self.driver.request(request)
        return re

    async def _handle_http(self, request: Request) -> Response:
        await self._forward(self.bots[self.adapter_config.wxid], request.json)
        return Response(200)

    @classmethod
    def payload_to_event(cls, payload: Dict[str, Any], adapter: "Adapter") -> Event:
        """
        转换Event
        当payload无法转换为Event时, 返回None
        """
        log("DEBUG", f"parse payload")
        try:
            raw = TestMessage.model_validate(payload)
        except ValidationError:
            try:
                raw = Message.model_validate(payload)
            except ValidationError as e:
                log("DEBUG", f"parse payload failed: {e}")
                return None
            
        # 过滤自身的消息
        if not adapter.adapter_config.self_msg and type(raw) == Message:
            if raw.TypeName in TypeName.AddMsg:
                if raw.Data.FromUserName.string == adapter.adapter_config.wxid:
                    return None
                
        event = Event.parse_event(raw)
        return event


    async def _forward(self, bot: Bot, payload: Dict):
        event = self.payload_to_event(payload, bot.adapter)
        # 让 bot 对事件进行处理
        if event:
            log("DEBUG", f"handle event: {event}")
            asyncio.create_task(bot.handle_event(event))
