import argparse
import asyncio

from xiaogpt.config import SUPPORTED_TTS, Config
from xiaogpt.providers import PROVIDERS
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
        "--deepseek_api_key",
        dest="deepseek_api_key",
        help="Deepseek api key",
    )
    parser.add_argument(
        "--mimo_api_key",
        dest="mimo_api_key",
        help="Xiaomi MiMo api key",
    )
    parser.add_argument(
        "--glm_api_key",
        dest="glm_api_key",
        help="Zhipu GLM api key",
    )
    parser.add_argument(
        "--proxy",
        dest="proxy",
        help="http proxy url like http://localhost:8080",
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
        choices=list(SUPPORTED_TTS),
    )
    bot_group = parser.add_mutually_exclusive_group()
    bot_group.add_argument(
        "--use_deepseek",
        dest="bot",
        action="store_const",
        const="deepseek",
        help="if use Deepseek api",
    )
    bot_group.add_argument(
        "--use_mimo",
        dest="bot",
        action="store_const",
        const="mimo",
        help="if use Xiaomi MiMo api",
    )
    bot_group.add_argument(
        "--use_glm",
        dest="bot",
        action="store_const",
        const="glm",
        help="if use Zhipu GLM api",
    )
    bot_group.add_argument(
        "--bot",
        dest="bot",
        help="bot type",
        choices=list(PROVIDERS),
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
        help="override the provider's official API base url",
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
