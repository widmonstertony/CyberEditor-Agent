# Security and Privacy / 安全与隐私

## Supported version

Security fixes currently target the latest `main` branch.

## Reporting

请不要在公开 issue 中粘贴客户素材、完整台词、访问令牌、绝对网络共享路径或
Resolve 工程数据库信息。发现安全问题时，请使用 GitHub 的私密漏洞报告功能。

Do not include customer media, full transcripts, access tokens, private network
paths, or Resolve database details in a public issue. Use GitHub private
vulnerability reporting for security-sensitive reports.

## Local-data boundary

CyberEditor-Agent sends director prompts only to the configured Ollama URL,
which defaults to `http://localhost:11434`. Changing `--ollama-url` to a remote
host intentionally changes the privacy boundary. Review that host before use.

媒体、字幕、关键帧和日志会写入本地 `data/`。这些运行时内容默认不会被 Git
跟踪，但用户仍应根据所在组织的数据保留政策进行加密、备份或删除。
