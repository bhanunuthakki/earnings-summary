/** Spreadsheet mechanics only. Python owns the complete financial formula spec. */
import fs from "node:fs/promises";
import crypto from "node:crypto";
import { pathToFileURL } from "node:url";
import { FileBlob, SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const digest = (bytes) => crypto.createHash("sha256").update(bytes).digest("hex");
const addressPattern = /^[A-Z]{1,3}[1-9][0-9]{0,3}$/;
const errors = /^#(?:REF!|DIV\/0!|VALUE!|NAME\?|N\/A|NUM!|NULL!|SPILL!|CALC!)/;
const sheetNames = ["Dashboard","Assumptions","Model","Valuation","Scenario","Actuals"];

export function authorWorkbook(spec) {
  if (spec.schema !== "meli-workbook-spec/v1" || !Array.isArray(spec.sheets)
      || spec.sheets.length !== 6 || !Array.isArray(spec.data_tables)
      || ![2,3].includes(spec.data_tables.length)) throw new Error("workbook_spec_invalid");
  const workbook = Workbook.create();
  const names = new Set();
  for (const sheet of spec.sheets) {
    if (typeof sheet.name !== "string" || !sheetNames.includes(sheet.name)
        || names.has(sheet.name)) throw new Error("workbook_sheet_invalid");
    names.add(sheet.name);
    workbook.worksheets.add(sheet.name);
  }
  for (const item of spec.sheets) {
    const sheet = workbook.worksheets.getItem(item.name);
    sheet.showGridLines = false;
    if (Object.keys(item.cells).length > 3000) throw new Error("workbook_cell_limit");
    for (const [address, value] of Object.entries(item.cells)) {
      if (!addressPattern.test(address) || !["number", "string"].includes(typeof value)
          || (typeof value === "number" && !Number.isFinite(value))) {
        throw new Error("workbook_cell_invalid");
      }
      const cell = sheet.getRange(address);
      if (typeof value === "string" && value.startsWith("=")) {
        cell.formulas = [[value]];
      } else cell.values = [[value]];
    }
    const used = sheet.getUsedRange();
    used.format.font = { name: "Arial", size: 10, color: "#222222" };
    used.format.verticalAlignment = "center";
    sheet.getRange("A1:A60").format.columnWidth = item.name === "Model" ? 3 : 48;
    sheet.getRange("B1:B60").format.columnWidth = item.name === "Model" ? 26 : 18;
    sheet.getRange("C1:G60").format.columnWidth = 19;
    sheet.getRange("A1:G1").format.font = {name:"Arial",size:14,bold:true};
    for (const [address, format] of Object.entries(item.formats)) {
      sheet.getRange(address).setNumberFormat(format);
    }
    for (const address of item.inputs) {
      sheet.getRange(address).format = {fill:"#FFF2CC",font:{color:"#0000FF"}};
    }
    for (const [address, value] of Object.entries(item.cells)) {
      if (typeof value === "string" && value.startsWith("=") && value.includes("!")) {
        sheet.getRange(address).format.font.color = "#008000";
      }
    }
  }
  const dashboard=workbook.worksheets.getItem("Dashboard");
  dashboard.getRange("E2").format.font.color = "#008000";
  dashboard.getRange("B44").dataValidation = {
    rule:{type:"whole",operator:"between",formula1:1,formula2:3}
  };
  dashboard.getRange("B32").dataValidation = {
    rule:{type:"decimal",operator:"greaterThan",formula1:0}
  };
  for (const address of ["D19","D27"]) {
    dashboard.getRange(address).dataValidation = {
      rule:{type:"decimal",operator:"between",formula1:0.000001,formula2:1}
    };
  }
  for (const table of spec.data_tables) {
    if (!names.has(table.sheet) || !/^[A-Z]+\d+:[A-Z]+\d+$/.test(table.range)
        || !addressPattern.test(table.row_input) || !addressPattern.test(table.column_input)) {
      throw new Error("workbook_data_table_invalid");
    }
    const sheet=workbook.worksheets.getItem(table.sheet);
    sheet.dataTables.add(table.range,{rowInput:table.row_input,columnInput:table.column_input});
  }
  dashboard.getRange("E47:G49").setNumberFormat("0.00");
  dashboard.getRange("E55:G57").setNumberFormat("0.00");
  for (const address of ["B37:B43","E47:G49","E55:G57"]) {
    dashboard.getRange(address).format.font.color = "#222222";
  }
  return workbook;
}

export function recalculateAndVerify(workbook, expected, expectedProvenance) {
  workbook.recalculate();
  // Data-table outputs need one dependent-formula refresh in this engine.
  // Keep the source formulas intact; do not replace their cached values.
  const scenario=workbook.worksheets.getItem("Scenario");
  for (const address of ["E3","E4","E5"]) {
    const cell=scenario.getRange(address);
    cell.formulas=cell.formulas;
  }
  workbook.recalculate();
  const provenance=scenario.getRange("B6").values[0][0];
  if (scenario.getRange("A6").values[0][0]!=="Bear provenance"
      || typeof provenance!=="string" || !provenance
      || (expectedProvenance!==undefined && provenance!==expectedProvenance)) {
    throw new Error("workbook_scenario_provenance_failed");
  }
  let checked=0;
  let maximumAbsoluteError=0;
  const outputs={};
  for (const [reference, value] of Object.entries(expected)) {
    const [name,address]=reference.split("!");
    const actual=workbook.worksheets.getItem(name).getRange(address).values[0][0];
    if (typeof actual !== "number" || !Number.isFinite(actual)
        || Math.abs(actual-value)>Math.max(1e-6,Math.abs(value)*1e-9)) {
      throw new Error(`workbook_numerical_parity_failed:${reference}`);
    }
    maximumAbsoluteError=Math.max(maximumAbsoluteError,Math.abs(actual-value));
    outputs[reference]=actual;
    checked++;
  }
  for (const name of sheetNames) {
    const sheet=workbook.worksheets.getItem(name);
    for (const row of sheet.getUsedRange().values) {
      if (row.some(value=>typeof value === "string" && errors.test(value))) {
        throw new Error("workbook_formula_error");
      }
    }
  }
  return {checked,maximum_absolute_error:maximumAbsoluteError,outputs,bear_provenance:provenance};
}

async function main() {
  const [input,destination]=process.argv.slice(2);
  if (!input || !destination) throw new Error("workbook_arguments_missing");
  const bytes=await fs.readFile(input);
  if (bytes.length>1024*1024) throw new Error("workbook_spec_too_large");
  const spec=JSON.parse(bytes.toString("utf8"));
  const workbook=authorWorkbook(spec);
  const before=recalculateAndVerify(workbook,spec.expected,spec.bear_provenance_note);
  await (await SpreadsheetFile.exportXlsx(workbook)).save(destination);
  const reopened=await SpreadsheetFile.importXlsx(await FileBlob.load(destination));
  const after=recalculateAndVerify(reopened,spec.expected,spec.bear_provenance_note);
  const receipt={schema:"meli-workbook-recalculation/v1",status:"verified",
    calculation_engine:"@oai/artifact-tool",node_version:process.version,
    spec_sha256:digest(bytes),workbook_sha256:digest(await fs.readFile(destination)),
    input_receipt_sha256:spec.input_receipt_sha256,
    input_recipe:spec.input_recipe,
    scenario_commitment_sha256:spec.scenario_commitment_sha256,
    financial_acceptance:"not_verified",native_excel_verified:false,
    before_export:before,after_reopen:after};
  await fs.writeFile(destination+".recalculation.json",JSON.stringify(receipt,null,2));
  console.log(JSON.stringify({status:receipt.status,checked:after.checked}));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(error=>{
    const message=String(error.message);
    console.error(/^workbook_[a-z_]+(?::[A-Za-z0-9!]+)?$/.test(message)
      ? message : "workbook_artifact_adapter_failed");
    process.exitCode=1;
  });
}
