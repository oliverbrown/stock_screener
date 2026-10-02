# Security Policy

## Supported versions

Only the latest commit on `main` is supported. There are no maintained
release branches.

## Reporting a vulnerability

Please **don't open a public issue** for a security problem.

Report it privately through GitHub: on the repository's **Security** tab,
choose **Report a vulnerability**
([direct link](https://github.com/oliverbrown/stock_screener/security/advisories/new)).
If that option isn't available to you, open an issue that says only that you
have a security report to share, without any details, and the maintainer
will arrange a private channel.

Please include:

- what the problem is and what it lets someone do;
- the steps or input that trigger it;
- the commit you tested.

You can expect an acknowledgement within about a week. This is a
personal project maintained in spare time, so fixes are made on a
best-effort basis; you'll be told what was decided either way, and credited
in the fix if you'd like.

## What counts

stock_screener is a command-line tool that runs locally. It needs no API
keys or accounts, and it only makes read-only requests to public data
sources (Yahoo Finance, Nasdaq, Wikipedia, and fund providers' holdings
files). Relevant reports include, for example:

- a crafted ticker file, `screens.toml`, cache file or downloaded response
  that causes code execution, writes outside the output / cache / log
  directories, or deletes files it shouldn't;
- HTML reports rendering untrusted data (e.g. a company name) as active
  content;
- a dependency with a known vulnerability that is reachable through this
  tool.

Wrong or misleading screening results are bugs rather than security issues
— please use a normal [bug report](https://github.com/oliverbrown/stock_screener/issues/new/choose).
