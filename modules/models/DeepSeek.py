from __future__ import annotations

import json
import logging
import traceback
import base64
from math import ceil

import colorama
import requests
from io import BytesIO
import uuid

import requests
from PIL import Image

from .. import shared
from ..config import retrieve_proxy, sensitive_id, usage_limit
from ..index_func import *
from ..presets import *
from ..utils import *
from .base_model import BaseLLMModel


class DeepSeek_Client(BaseLLMModel):
    def __init__(
        self,
        model_name,
        api_key,
        user_name=""
    ) -> None:
        super().__init__(
            model_name=model_name,
            user=user_name,
            config={
                "api_key": api_key
            }
        )
        # 设置DeepSeek API的基础URL
        self.api_host = os.environ.get("DEEPSEEK_API_HOST", self.api_host or "api.deepseek.com")
        self.chat_completion_url = f"https://{self.api_host}/chat/completions"
        self.base_url = f"https://{self.api_host}"
        self._refresh_header()
        logging.info(f"DeepSeek客户端初始化完成，API地址: {self.chat_completion_url}")

    def get_answer_stream_iter(self):
        logging.info("开始流式获取回答")
        response = self._get_response(stream=True)
        if response is not None:
            logging.info(f"收到响应，状态码: {response.status_code}")
            if response.status_code != 200:
                logging.error(f"API返回错误: {response.text}")
                yield f"API返回错误: {response.status_code} - {response.text}"
                return
                
            iter = self._decode_chat_response(response)
            partial_text = ""
            for i in iter:
                if i is not None:  # 添加检查，确保i不是None
                    partial_text += i
                    yield partial_text
        else:
            logging.error("未收到响应")
            yield STANDARD_ERROR_MSG + GENERAL_ERROR_MSG

    def get_answer_at_once(self):
        logging.info("开始一次性获取回答")
        response = self._get_response()
        if response is None:
            logging.error("未收到响应")
            return STANDARD_ERROR_MSG + GENERAL_ERROR_MSG, 0
            
        if response.status_code != 200:
            logging.error(f"API返回错误: {response.text}")
            return f"API返回错误: {response.status_code} - {response.text}", 0
            
        try:
            response_json = json.loads(response.text)
            logging.info(f"收到响应: {response_json}")
            content = response_json["choices"][0]["message"]["content"]
            total_token_count = response_json["usage"]["total_tokens"]
            return content, total_token_count
        except Exception as e:
            logging.error(f"解析响应时出错: {e}")
            logging.error(f"响应内容: {response.text}")
            return f"解析响应时出错: {e}", 0

    def _refresh_header(self):
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        logging.info("已刷新请求头")

    @shared.state.switching_api_key
    def _get_response(self, stream=False):
        deepseek_api_key = self.api_key
        system_prompt = self.system_prompt
        history = self.history

        logging.debug(colorama.Fore.YELLOW +
                      f"历史记录: {history}" + colorama.Fore.RESET)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {deepseek_api_key}",
        }

        if system_prompt is not None:
            history = [construct_system(system_prompt), *history]

        payload = {
            "model": self.model_name,
            "messages": history,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "n": self.n_choices,
            "stream": stream,
        }

        if self.max_generation_token:
            payload["max_tokens"] = self.max_generation_token
        if self.presence_penalty:
            payload["presence_penalty"] = self.presence_penalty
        if self.frequency_penalty:
            payload["frequency_penalty"] = self.frequency_penalty
        if self.stop_sequence:
            payload["stop"] = self.stop_sequence
        if self.logit_bias is not None:
            payload["logit_bias"] = self.encoded_logit_bias()
        if self.user_identifier:
            payload["user"] = self.user_identifier

        if stream:
            timeout = TIMEOUT_STREAMING
        else:
            timeout = TIMEOUT_ALL

        logging.info(f"准备发送请求到 {self.chat_completion_url}，流式响应: {stream}")
        logging.debug(f"请求负载: {payload}")
        
        with retrieve_proxy():
            try:
                response = requests.post(
                    self.chat_completion_url,
                    headers=headers,
                    json=payload,
                    stream=stream,
                    timeout=timeout,
                )
                logging.info(f"请求已发送，状态码: {response.status_code}")
                return response
            except Exception as e:
                logging.error(f"发送请求时出错: {e}")
                traceback.print_exc()
                return None

    def _decode_chat_response(self, response):
        logging.info("开始解码聊天响应")
        error_msg = ""
        content_received = False
        
        # 将响应头信息的日志级别从INFO降级为DEBUG
        logging.debug(f"响应头: {response.headers}")
        
        # 简单的流式处理方法
        for line in response.iter_lines():
            if not line:
                continue
                
            try:
                line_str = line.decode('utf-8')
                logging.debug(f"原始行: {line_str}")
                
                # 跳过HTTP头信息
                if line_str.startswith(':') or line_str == 'keep-alive':
                    continue
                    
                # 处理SSE格式
                if line_str.startswith('data: '):
                    data = line_str[6:]  # 去掉 "data: " 前缀
                    
                    if data == '[DONE]':
                        logging.info("收到[DONE]标记")
                        break
                        
                    try:
                        json_data = json.loads(data)
                        
                        # 检查是否有choices
                        if 'choices' in json_data and json_data['choices']:
                            choice = json_data['choices'][0]
                            
                            # 检查是否有delta
                            if 'delta' in choice:
                                delta = choice['delta']
                                
                                # 检查是否有content
                                if 'content' in delta and delta['content'] is not None:
                                    content_received = True
                                    logging.debug(f"收到内容: {delta['content']}")
                                    yield delta['content']
                                # 检查是否有reasoning_content
                                elif 'reasoning_content' in delta and delta['reasoning_content'] is not None:
                                    content_received = True
                                    logging.debug(f"收到推理内容: {delta['reasoning_content']}")
                                    yield delta['reasoning_content']
                    except json.JSONDecodeError:
                        logging.error(f"JSON解析错误: {data}")
                    except Exception as e:
                        logging.error(f"处理JSON时出错: {e}")
            except Exception as e:
                logging.error(f"处理行时出错: {e}")
                continue
        
        if not content_received:
            logging.warning("未收到任何内容")
            yield "未能从API获取回答，请尝试使用非流式响应模式。"
        
        logging.info("解码完成")

    def set_key(self, new_access_key):
        logging.info("设置新的API密钥")
        ret = super().set_key(new_access_key)
        self._refresh_header()
        return ret 