const SPREADSHEET_ID = "11VhtqvCxl_IVk9EJgzbAFcO2J50iiq_gy24ftZjLfBs";
const WEBAPP_TOKEN = "change_me";
const SUMMARY_SHEET_NAME = "全部反馈";

function doPost(e) {
  try {
    const payload = JSON.parse(e.postData.contents || "{}");

    if (WEBAPP_TOKEN && payload.token !== WEBAPP_TOKEN) {
      return jsonResponse({ ok: false, error: "invalid token" });
    }

    const headers = payload.headers || [];
    const records = payload.records || [];
    const ss = SpreadsheetApp.openById(payload.spreadsheet_id || SPREADSHEET_ID);

    const rowsByCompany = {};
    const allRows = records.map(recordToRow);

    records.forEach(record => {
      const company = record.company || "未确认公司";
      if (!rowsByCompany[company]) {
        rowsByCompany[company] = [];
      }
      rowsByCompany[company].push(recordToRow(record));
    });

    writeSheet(ss, SUMMARY_SHEET_NAME, headers, allRows);

    Object.keys(rowsByCompany).sort().forEach(company => {
      writeSheet(ss, safeSheetName(company), headers, rowsByCompany[company]);
    });

    return jsonResponse({
      ok: true,
      spreadsheet_id: ss.getId(),
      record_count: records.length,
      company_count: Object.keys(rowsByCompany).length,
    });
  } catch (err) {
    return jsonResponse({ ok: false, error: String(err && err.stack ? err.stack : err) });
  }
}

function recordToRow(record) {
  return [
    record.session_id || "",
    record.updated_at || "",
    record.company || "",
    record.name || "",
    record.title || "",
    record.responsibility || "",
    record.flow || "",
    record.frequency || "",
    record.time_cost || "",
    record.error_impact || "",
    record.current_tools || "",
    record.improvement || "",
    record.data_rules || "",
    record.out_of_scope || "",
    record.transcript || "",
  ];
}

function writeSheet(ss, name, headers, rows) {
  let sheet = ss.getSheetByName(name);
  if (!sheet) {
    sheet = ss.insertSheet(name);
  }

  const existingFilter = sheet.getFilter();
  if (existingFilter) {
    existingFilter.remove();
  }

  sheet.clear();

  const values = [headers].concat(rows);
  if (values.length > 0 && headers.length > 0) {
    sheet.getRange(1, 1, values.length, headers.length).setValues(values);
  }

  if (headers.length > 0) {
    const headerRange = sheet.getRange(1, 1, 1, headers.length);
    headerRange.setBackground("#1F4E78");
    headerRange.setFontColor("#FFFFFF");
    headerRange.setFontWeight("bold");
    sheet.setFrozenRows(1);
    sheet.getRange(1, 1, Math.max(values.length, 1), headers.length).createFilter();
  }

  const widths = [150, 150, 220, 120, 140, 220, 360, 160, 220, 260, 260, 260, 300, 260, 600];
  widths.forEach((width, index) => sheet.setColumnWidth(index + 1, width));
  sheet.getDataRange().setWrap(true).setVerticalAlignment("top");
}

function safeSheetName(company) {
  let name = String(company || "未确认公司").replace(/[\[\]\*:/\\?]/g, "_").trim();
  if (!name) {
    name = "未确认公司";
  }
  return name.substring(0, 31);
}

function jsonResponse(body) {
  return ContentService
    .createTextOutput(JSON.stringify(body))
    .setMimeType(ContentService.MimeType.JSON);
}
