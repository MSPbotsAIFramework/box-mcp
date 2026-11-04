# Copyright 2026 MSPbots.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Process settings. Holds no credentials: those arrive per request as headers."""

from pydantic_settings import BaseSettings, SettingsConfigDict

API_BASE_URL = "https://api.box.com/2.0"
UPLOAD_BASE_URL = "https://upload.box.com/api/2.0"
TOKEN_URL = "https://api.box.com/oauth2/token"
DEFAULT_LIMIT = 25
MAX_LIMIT = 200
# Box's own simple-upload ceiling is 50 MB; v1 of this connector caps well under it
# because base64-encoding a 50 MB file into one MCP tool call is impractical for an
# LLM-driven caller (see docs/tickets/PRD-21298.md "Option analysis").
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class Settings(BaseSettings):
    # extra="ignore": the runtime injects variables this project does not declare.
    model_config = SettingsConfigDict(extra="ignore")

    mcp_http_port: int = 8080
    mcp_http_host: str = "0.0.0.0"
    log_level: str = "INFO"


def get_settings() -> Settings:
    return Settings()
