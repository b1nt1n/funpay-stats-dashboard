# Security

`golden_key` is an authorization cookie. Treat it like a password.

## What not to publish

- `golden_key` values.
- Full `Cookie` headers.
- Generated reports such as `funpay_stats.html`.
- Saved FunPay order/profile HTML files.

Generated reports can contain private order history, usernames, item names,
amounts, and categories. The repository `.gitignore` excludes common generated
and local-secret files, but check your upload list before publishing an archive
or release.

## Network behavior

The script sends requests only to `funpay.com` to load profile and order pages.
The generated HTML dashboard works locally and may load the profile avatar from
FunPay CDN if the profile has an avatar URL.

## If a key leaks

Log out of FunPay, log back in, and use the new `golden_key`.
