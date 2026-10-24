function toggleColumns(tableId, fieldContainer) {
    var table = $(`#${tableId}[data-table-name]`)
    table.hide()
    var tableTitle = table.attr("data-table-name")
    let trs = []
    $.each(table.find("[data-cell-title]"), function (indexInArray, valueOfElement) {
        trs.push(`
            <li class="oh-dropdown__item oh-sticy-dropdown-item">
                <span>${$(valueOfElement).attr("data-cell-title")}</span>
                <span class="oh-table__checkbox">
                    <input type="checkbox" name="showTableColumn" onchange="hideCells($(this),'${tableTitle}','${fieldContainer}')" value="${$(valueOfElement).attr("data-cell-index")}"/>
                </span>
            </li>
         `)
    });

    // toggle cells
    var visibleCells = localStorage.getItem(tableTitle)
    var defaultVisibleCells = table.attr("data-default-visible-cells")
    var storedIds =
        visibleCells === null
            ? defaultVisibleCells === undefined
                ? null
                : defaultVisibleCells.split(",")
            : JSON.parse(visibleCells)
    var storedIdStrings = storedIds === null ? null : storedIds.map(String)
    table.toggleClass("employee-attendance-columns-customized", visibleCells !== null)
    table.find("[data-cell-index]").each(function () {
        $(this).toggle(
            storedIdStrings === null || storedIdStrings.includes(String($(this).attr("data-cell-index")))
        )
    })

    var trsString = ""
    for (let tr = 0; tr < trs.length; tr++) {
        const element = trs[tr];
        trsString = trsString + element
    }
    let selectButtons = $(`
    <div class="oh-dropdown_btn-header">
    <button onclick="$(this).parent().parent().find('[type=checkbox]').prop('checked',true).change()" class="oh-btn oh-btn--success-outline">Select All Columns</button>
    <button onclick="$(this).parent().parent().find('[type=checkbox]').prop('checked',false).change()" class="oh-btn oh-btn--primary-outline">Unselect All Columns</button>
    </div>
    `)
    $(`#${fieldContainer}`).parent().prepend(selectButtons)
    $(`#${fieldContainer}`).html(trsString);
    $(`#${fieldContainer} input[type=checkbox]`).each(function () {
        $(this).prop(
            "checked",
            storedIdStrings === null || storedIdStrings.includes(String($(this).val()))
        )
    })
    table.show();
}
function hideCells(jqElement, tableTitle, fieldContainer) {
    var visibleCells = $(`#${fieldContainer}`).find("input[type=checkbox]:checked")
    var visibleCellsids = []
    var table = $(`[data-table-name="${tableTitle}"]`)
    table.addClass("employee-attendance-columns-customized")
    table.find("[data-cell-index]").hide();
    $.each(visibleCells, function (indexInArray, valueOfElement) {
        table.find(`[data-cell-index="${$(valueOfElement).val()}"]`).show();
        visibleCellsids.push($(valueOfElement).val())
    });
    if (jqElement.is(":checked")) {
        var storedIdsSet = new Set(JSON.parse(localStorage.getItem(tableTitle)) || []);
        storedIdsSet.add(jqElement.val());
        var storedIds = Array.from(storedIdsSet);
        localStorage.setItem(tableTitle, JSON.stringify(storedIds));
    } else {
        var storedIds = JSON.parse(localStorage.getItem(tableTitle)) || [];
        var index = storedIds.indexOf(jqElement.val());
        if (index !== -1) {
            storedIds.splice(index, 1);
            localStorage.setItem(tableTitle, JSON.stringify(storedIds));
        }
    }
    table.show();
}
