"""运行路由、内部指令与静音所有权回归测试。"""

import unittest

from tests.test_tts_pipeline import MuteOwnershipTests, RouteAndDirectiveTests

if __name__ == "__main__":
    unittest.main(verbosity=2)
