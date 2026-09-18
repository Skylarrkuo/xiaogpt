import argparse
import asyncio

from xiaogpt.config import Config
from xiaogpt.xiaogpt import MiGPT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--hardware",
        dest="hardware",
        help="小爱 hardware",
    )
    parser.add_argument(
        "--account",
        dest="account",
        help="xiaomi account",
    )
    parser.add_argument(
        "--password",
        dest="password",
        help="xiaomi password",
    )
    parser.add_argument(
        "--openai_key",
        dest="openai_key",
        help="openai api key",
    )
    parser.add_argument(
        "--gemini_key",
        dest="gemini_key",
        help="gemini api key",
    )
    parser.add_argument(
        "--gemini_api_domain",
        dest="gemini_api_domain",
        help="custom gemini api domain",
    )
    parser.add_argument(
        "--deepseek_api_key",
        dest="deepseek_api_key",
        help="Deepseek api key",
    )
    parser.add_argument(
        "--proxy",
        dest="proxy",
        help="http proxy url like http://localhost:8080",
    )
    parser.add_argument(
        "--cookie",
        dest="cookie",
        help="xiaomi cookie",
    )
    parser.add_argument(
        "--stream",
        dest="stream",
        action="store_true",
        default=None,
        help="GPT stream mode",
    )
    parser.add_argument(
        "--use_command",
        dest="use_command",
        action="store_true",
        default=None,
        help="use command to tts",
    )
    parser.add_argument(
        "--mute_xiaoai",
        dest="mute_xiaoai",
        action="store_true",
        default=None,
        help="try to mute xiaoai answer",
    )
    parser.add_argument(
        "--volc_access_key", dest="volc_access_key", help="Volcengine access key"
    )
    parser.add_argument(
        "--volc_secret_key", dest="volc_secret_key", help="Volcengine secret key"
    )
    # for fish tts
    parser.add_argument("--fish_api_key", dest="fish_api_key", help="fish api key")
    parser.add_argument(
        "--fish_voice_key", dest="fish_voice_key", help="fish voice key"
    )
    parser.add_argument(
        "--verbose",
        "-v",
        dest="verbose",
        action="count",
        default=0,
        help="show info",
    )
    parser.add_argument(
        "--tts",
        help="TTS provider",
        choices=["mi", "edge", "openai", "azure", "google", "baidu", "volc", "fish"],
    )
    bot_group = parser.add_mutually_exclusive_group()
    bot_group.add_argument(
        "--use_chatgpt_api",
        dest="bot",
        action="store_const",
        const="chatgptapi",
        help="if use openai chatgpt api",
    )
    bot_group.add_argument(
        "--use_gemini",
        dest="bot",
        action="store_const",
        const="gemini",
        help="if use gemini",
    )
    bot_group.add_argument(
        "--use_doubao",
        dest="bot",
        action="store_const",
        const="doubao",
        help="if use doubao",
    )
    bot_group.add_argument(
        "--use_deepseek",
        dest="bot",
        action="store_const",
        const="deepseek",
        help="if use Deepseek api",
    )
    bot_group.add_argument(
        "--bot",
        dest="bot",
        help="bot type",
        choices=[
            "chatgptapi",
            "deepseek",
            "gemini",
            "doubao",
        ],
    )
    parser.add_argument(
        "--config",
        dest="config",
        help="config file path",
    )
    # args to change api_base
    parser.add_argument(
        "--api_base",
        dest="api_base",
        help="specify base url other than the OpenAI's official API address",
    )

    parser.add_argument(
        "--deployment_id",
        dest="deployment_id",
        help="specify deployment id, only used when api_base points to azure",
    )

    options = parser.parse_args()
    config = Config.from_options(options)

    async def main(config: Config) -> None:
        miboy = MiGPT(config)
        try:
            await miboy.run_forever()
        finally:
            await miboy.close()

    loop = asyncio.get_event_loop()
    loop.run_until_complete(main(config))


if __name__ == "__main__":
    main()
