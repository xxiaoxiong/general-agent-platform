"""文档服务统一错误，API 将状态和安全提示交给调用方。"""


class DocumentError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


class StructuredDocumentError(DocumentError):
    """文档模型回包可从已保存的图节点再次尝试。"""

    code = "document_model_output_invalid"

    def __init__(self, issue: str, *, stage: str = ""):
        self.issue = issue
        detail = "模型未返回符合文档规范的结构化内容"
        if issue == "truncated":
            detail = "模型输出未完整结束"
        elif issue == "empty":
            detail = "模型没有返回可用正文"
        super().__init__(422, f"{stage}。{detail}。" if stage else f"{detail}。")
