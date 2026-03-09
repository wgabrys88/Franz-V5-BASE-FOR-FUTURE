Coding Rules:

- Python 3.13 only. Modern syntax, strict typing with dataclasses and pattern matching. No legacy code, no compatibility shims.
- Windows 11 only. No cross-platform fallbacks or compatibility layers.
- Latest Google Chrome browser support only.
- OpenAI-compatible /chat/completions API is used by design (stateless).
- Maximum code reduction. Remove every possible line while keeping 100% original functionality.
- Perfect Pylance/pyright compatibility: full type hints, frozen dataclasses for all configuration.
- No comments anywhere in any file. Code blocks must contain no non-ASCII characters.
- No slicing or truncating of data anywhere in the code. Python code must not impact the data in any way, not even as a safety mechanism.
- No functional "magic values" - all constants must live in frozen dataclasses.
- .html files must use latest HTML5 + modern CSS (custom properties, grid/flex) + modern JS. All HTML files must follow the dark cyber aesthetic established in the project. All HTML files are meant to be displayed in latest Google Chrome on 1080p 16:9 ratio screen. Ensure that .html files are going to be modern without any legacy support - similar code rules to python, minimalism (code reduction) and modern design only.
- Optimize every prompt for the small 0.8B–2B Qwen3.5 VL model: short, explicit, format-enforcing, minimal chain-of-thought.
- Write prompts in form of three quote (docstring) type of comments, do not make a multiline prompt created as independent strings with end of the line characters at the end - its very hard to maintain that way)
- Ensure the code contains no duplications and no dead functionality. Remove any functional fallbacks.