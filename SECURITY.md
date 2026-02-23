# Security Policy

## Supported Versions

このリポジトリは rolling release で運用し、`main` ブランチの最新をサポート対象とします。

## Reporting a Vulnerability

脆弱性の報告は公開Issueではなく、以下で受け付けます。

- GitHub Security Advisory（推奨）
- または運用担当の連絡先へ直接連絡

受領後の目安:

1. **48時間以内**に初回応答
2. **7日以内**にトリアージ結果の共有
3. 重大度に応じて修正と公開調整（必要に応じCVE採番）

## Security Baseline

本プロジェクトの最低運用基準:

- コンテナは非root・`read_only`・`cap_drop: ALL`・`no-new-privileges` を維持
- CIで `python -m py_compile main.py` と `unittest` を必須化
- CodeQLを週次/PRで実行
- Dependabotで Docker / GitHub Actions の更新を週次追従

## Release and Patch Policy

- 重大なセキュリティ修正は通常リリースを待たずにパッチリリース
- 修正後は再発防止としてテストを追加
- 可能な限り後方互換性を維持し、破壊的変更はREADMEに明記
