# ECMA-376 5th Edition schemas

本目录用于离线校验 `ppt-report` 生成的 OOXML Transitional 演示文稿部件。

- PresentationML、DrawingML 和扩展属性 XSD 来自 ECMA 官方的
  [ECMA-376 Part 4, 5th edition (December 2016)](https://ecma-international.org/wp-content/uploads/ECMA-376-4_5th_edition_december_2016.zip)，
  压缩包内文件为 `OfficeOpenXML-XMLSchema-Transitional.zip`。
- OPC 内容类型、关系和核心属性 XSD 来自 ECMA 官方的
  [ECMA-376 Part 2, 5th edition (December 2021)](https://ecma-international.org/wp-content/uploads/ECMA-376-2_5th_edition_december_2021.zip)，
  压缩包内文件为 `OpenPackagingConventions-XMLSchema.zip`。
- Dublin Core 依赖分别来自 DCMI 官方的
  [`dc.xsd`](https://www.dublincore.org/schemas/xmls/qdc/2003/04/02/dc.xsd)、
  [`dcterms.xsd`](https://www.dublincore.org/schemas/xmls/qdc/2003/04/02/dcterms.xsd) 和
  [`dcmitype.xsd`](https://www.dublincore.org/schemas/xmls/qdc/2003/04/02/dcmitype.xsd)。
- XML namespace schema 来自 W3C 官方的
  [`xml.xsd`](https://www.w3.org/2001/xml.xsd)。
- XSD 的 schema 定义保持官方版本不变；仅移除 UTF-8 BOM、将 CRLF 行尾规范化为
  仓库使用的 LF，并作以下离线 `schemaLocation` 本地化：
  - `opc-coreProperties.xsd`：两个 Dublin Core URL 分别改为 `dc.xsd` 和 `dcterms.xsd`；
  - `dc.xsd`、`dcterms.xsd`：W3C XML schema URL 改为 `xml.xsd`。

覆盖范围：`pml.xsd` 及其完整本地依赖闭包、`theme*.xml`、`docProps/app.xml`、
`docProps/core.xml`、所有 `.rels` 和 `[Content_Types].xml`。全部依赖均在本目录，
校验过程不需要联网。
