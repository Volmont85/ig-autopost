"""Публикация фото, каруселей, роликов и историй в Instagram через Graph API."""
import json
import os
import sys
import time

import requests

API_VERSION = "v21.0"
BASE_URL = f"https://graph.instagram.com/{API_VERSION}"


class IGPublishError(Exception):
    pass


class Account:
    def __init__(self, name, ig_id, token):
        self.name = name
        self.ig_id = ig_id
        self.token = token

    @classmethod
    def from_env(cls, name):
        prefix = f"IG_{name.upper()}"
        ig_id = os.environ.get(f"{prefix}_ID")
        token = os.environ.get(f"{prefix}_TOKEN")
        if not ig_id or not token:
            raise IGPublishError(f"Не заданы {prefix}_ID / {prefix}_TOKEN в переменных окружения")
        return cls(name, ig_id, token)

    def _request(self, method, path, **params):
        params["access_token"] = self.token
        response = requests.request(method, f"{BASE_URL}/{path}", params=params, timeout=60)
        data = response.json()
        if "error" in data:
            raise IGPublishError(data["error"].get("message", str(data["error"])))
        return data

    def _create_container(self, **params):
        return self._request("POST", f"{self.ig_id}/media", **params)["id"]

    def _wait_until_ready(self, creation_id, timeout=300, interval=5):
        elapsed = 0
        while elapsed < timeout:
            status = self._request("GET", creation_id, fields="status_code").get("status_code")
            if status == "FINISHED":
                return
            if status == "ERROR":
                raise IGPublishError(f"Контейнер {creation_id} завершился с ошибкой обработки")
            time.sleep(interval)
            elapsed += interval
        raise IGPublishError(f"Контейнер {creation_id} не обработался за {timeout} секунд")

    def _publish(self, creation_id):
        return self._request("POST", f"{self.ig_id}/media_publish", creation_id=creation_id)["id"]

    def publish_photo(self, image_url, caption=""):
        creation_id = self._create_container(image_url=image_url, caption=caption)
        self._wait_until_ready(creation_id, timeout=60, interval=2)
        return self._publish(creation_id)

    def publish_carousel(self, image_urls, caption=""):
        if not (2 <= len(image_urls) <= 10):
            raise IGPublishError("Карусель должна содержать от 2 до 10 элементов")
        children = [self._create_container(image_url=url, is_carousel_item="true") for url in image_urls]
        creation_id = self._create_container(
            media_type="CAROUSEL", children=",".join(children), caption=caption
        )
        self._wait_until_ready(creation_id, timeout=60, interval=2)
        return self._publish(creation_id)

    def publish_reel(self, video_url, caption="", cover=None, share_to_feed=True,
                      trial=False, graduation_strategy="MANUAL"):
        params = {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": "true" if share_to_feed else "false",
        }
        if cover:
            params["cover_url"] = cover
        if trial:
            params["trial_params"] = json.dumps({"graduation_strategy": graduation_strategy})
        creation_id = self._create_container(**params)
        self._wait_until_ready(creation_id)
        return self._publish(creation_id)

    def publish_story(self, media_url, media_type="photo"):
        params = {"media_type": "STORIES"}
        if media_type == "video":
            params["video_url"] = media_url
        else:
            params["image_url"] = media_url
        creation_id = self._create_container(**params)
        timeout = 300 if media_type == "video" else 60
        self._wait_until_ready(creation_id, timeout=timeout, interval=2)
        return self._publish(creation_id)

    def quota(self):
        data = self._request("GET", f"{self.ig_id}/content_publishing_limit", fields="config,quota_usage")
        items = data.get("data", [])
        return items[0] if items else {}

    def delete_media(self, media_id):
        # Не подтверждено официальной документацией, что DELETE работает в
        # потоке "Instagram API with Instagram Login" (не Facebook Login) —
        # проверяется эмпирически реальным вызовом.
        return self._request("DELETE", media_id)


def _main():
    if len(sys.argv) < 3:
        print("Использование: python ig_publish.py <account> quota")
        sys.exit(1)

    account = Account.from_env(sys.argv[1])
    command = sys.argv[2]

    if command == "quota":
        print(account.quota())
    else:
        print(f"Неизвестная команда: {command}")
        sys.exit(1)


if __name__ == "__main__":
    _main()
