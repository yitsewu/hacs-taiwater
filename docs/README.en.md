# HACS — Taiwan Water Corporation bill import

[繁體中文](../README.md)

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=yitsewu&repository=taiwan-water&category=integration)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![MIT License](https://img.shields.io/badge/License-MIT-blue.svg)](../LICENSE)
[![Crawler test](https://img.shields.io/github/actions/workflow/status/yitsewu/taiwan-water/live-check.yaml?branch=main&label=Crawler%20test)](https://github.com/yitsewu/taiwan-water/actions/workflows/live-check.yaml)

View Taiwan Water Corporation bills, water consumption and costs in Home Assistant. Enter the water account ID and customer name printed on your bill to get started.

This is an unofficial integration for Taiwan Water Corporation. It does not support Taipei Water Department. Built-in OCR runs locally in HA; no separate OCR service or add-on is required.

[Full usage guide (Chinese)](usage.md) · [Crawler schedule and status (Chinese)](live-check.md) · [Changelog (Chinese)](../CHANGELOG.md)

## Features

- Bill details, consumption, costs and saved bill history.
- Scheduled queries, latest-bill queries, history backfill and statistics rebuilding.
- Last/next query times, query results and separate OCR, verification and statistics status.
- Consumption and cost statistics for the Energy dashboard, without custom helpers or YAML.
- Multiple accounts and custom display names; failed queries preserve previously saved bills.

## Installation

Requires **Home Assistant 2026.9.1 or later**. Core 2026.9.1 has been tested on amd64 and aarch64; other versions and 32-bit platforms have not been verified.

**HACS**

1. Install HACS, then use the button above. Alternatively, add `https://github.com/yitsewu/taiwan-water` under HACS → Custom repositories and select **Integration**.
2. Download the integration and restart Home Assistant.

**ZIP installation**

1. Open [Releases](https://github.com/yitsewu/taiwan-water/releases). Download `taiwater-VERSION.zip` and copy the complete `custom_components/taiwater` folder, including `models`, into `/config/custom_components/`.
2. Restart Home Assistant.

Back up HA before upgrading. Existing accounts and saved history are retained; you do not need to enter your water ID again.

## Account setup

1. Open **Settings → Devices & services → Add integration** and search for **台灣自來水公司**.
2. Enter the water account ID and customer name printed on your bill. You can customize the display name. Initial import defaults to all bill periods available on the official website.
3. Adjust the query schedule in integration options. The default is **Monday at 09:00**, using your HA time zone.
4. Wait for long-term statistics to finish, then configure the Energy dashboard below.

## Add to the Energy dashboard

Wait until **Long-term statistics status** shows completed. Open **Energy → Edit dashboard → Water → Add water source**. Labels may vary with the HA language and version; the screenshots show the Traditional Chinese interface.

| ① Select consumption | ② Add costs |
| :--- | :--- |
| Select **台灣自來水公司 總用水量** (total water consumption). Leave the flow-rate field empty. The display name is optional. | Select **Use an entity tracking the total costs**, then choose **台灣自來水公司 總費用** (total cost) and save. |
| <a href="images/energy-water-usage.png"><img src="images/energy-water-usage.png" alt="Select total water consumption and leave flow rate empty" width="300"></a> | <a href="images/energy-water-cost.png"><img src="images/energy-water-cost.png" alt="Select the integration total cost statistic" width="300"></a> |

Click an image to view it at full size. These screenshots show an existing source, so Save is disabled until a change is made. If you customized the account name, select the statistics with that name.

Return to **Energy → Water** and select a month or year covered by your bills. A newly added source can take up to two hours to appear. The chart shows allocated bill consumption, not live meter readings.

## Automatic queries

| Purpose | Runs on | Schedule and scope |
| --- | --- | --- |
| Import your bills | Home Assistant | Monday at 09:00 by default; configurable daily, weekly or monthly in the HA time zone. Fills missing periods and refreshes the latest bill. |
| Public compatibility check | GitHub Actions | Every six hours, at 02:23, 08:23, 14:23 and 20:23 Taiwan time; checks the latest stable release built-in OCR, website verification and bill parser. It does not write to HA. |

Use integration options to change the schedule or the automatic-query switch to pause it. A monthly date beyond the end of the month runs on the last day. GitHub schedules may be delayed; the badge is independent of your home HA query status. See [check scope and status (Chinese)](live-check.md).

## Bill data

| Data | Description |
| --- | --- |
| Bill period and consumption | Latest bill period, billed water volume and available history |
| Charges and dates | Original total, charge details and dates supplied by the website |
| Payment status | Shown when provided; missing information remains unknown |
| Monthly and annual summaries | Allocated consumption, costs, optional carbon estimates and covered months; not necessarily a complete year |

Saved history is retained when the website stops listing an old bill. Backfill can refresh previously saved bills within the selected history range.

## Monthly allocation

New accounts default to equal allocation across the months covered by a bill. For example, **60 m³ and TWD 600 → 30 m³ and TWD 300 per month** for a two-month period.

This is **an estimate based on bill allocation, not actual monthly meter readings**. You can switch to allocation by billing-period days. Rebuild long-term statistics to recalculate saved bills without querying the website. Existing accounts retain their allocation setting; changing the method rebuilds statistics without changing their IDs.

Carbon data uses website values when available, or a configured factor with its source and applicable year. Missing data remains unknown.

## Troubleshooting

- **Query latest bill** refreshes the latest available bill.
- **Backfill history** retrieves or refreshes bills in the configured history range.
- **Rebuild long-term statistics** recalculates saved bills without contacting the website or running OCR.
- Check query and verification status after a failure. OCR success does not mean the website accepted the result; manual verification fallback is available.
- If bills are available but the Energy dashboard has not updated, check long-term statistics status and wait for completion or rebuild statistics.

See the [full usage guide (Chinese)](usage.md) for options and detailed troubleshooting.

## Data source

Data comes from the [Taiwan Water bill-query website](https://www.water.gov.tw/ch/EQuery/WaterFeeQuery?nodeId=753). The integration uses your water account ID and customer name, follows the website verification process, and organizes bill details into HA consumption, cost and historical statistics.

## Report a problem

Open an [issue](https://github.com/yitsewu/taiwan-water/issues). Do not include account IDs, customer names, cookies, verification codes or original bills.

## License

Code is licensed under the [MIT License](../LICENSE).

The bundled OCR model retains its [upstream MIT license](../custom_components/taiwater/models/LICENSE.ddddocr.txt) and [source and integrity information](../custom_components/taiwater/models/source.json).

The integration icon uses the official Taiwan Water Corporation logo. Its [source and integrity information](../custom_components/taiwater/brand/source.json) is retained. Rights to the logo belong to Taiwan Water Corporation; it is not covered by the code’s MIT license. It identifies the connected service and does not imply official integration or endorsement.
