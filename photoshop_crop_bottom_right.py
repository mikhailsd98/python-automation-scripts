# -*- coding: utf-8 -*-
# pip install pywin32
# Usage:
#   python photoshop_crop_bottom_right.py "C:\\path\\to\\image.jpg" [size_or_scale] [format=PNG|JPEG] [mode=fixed|scale] [margin_left] [margin_bottom]
#   Примеры:
#     - Масштаб (по умолчанию 1/3 от меньшей стороны) с отступами 20px:  
#       python photoshop_crop_bottom_right.py "C:\\img.jpg"
#     - Масштаб 1/4 и отступы 30px слева и 30px снизу:  
#       python photoshop_crop_bottom_right.py "C:\\img.jpg" 4 PNG scale 30 30
#     - Фиксированный размер 256px, отступы 10px и 20px:  
#       python photoshop_crop_bottom_right.py "C:\\img.jpg" 256 PNG fixed 10 20
#   Параметры: <путь_к_изоб> [size_or_scale] [format] [mode] [margin_left] [margin_bottom]

import os
import sys
import time

try:
    import win32com.client as win32
    from win32com.client import gencache
    import pythoncom
    import pywintypes
except Exception as e:
    win32 = None
    gencache = None
    pythoncom = None
    pywintypes = None


def run_photoshop_crop(
    image_path: str,
    size_or_scale: int | float = 3,  # если mode='scale' — делитель (3 => 1/3), если 'fixed' — пиксели
    out_suffix: str = "_filled",
    out_format: str = "PNG",  # "PNG" или "JPEG"
    mode: str = "scale",       # 'scale' (по умолчанию) или 'fixed'
    margin_left: int = 20,
    margin_bottom: int = 20,
    ps=None,
) -> str:
    if not os.path.isfile(image_path):
        raise FileNotFoundError(f"Файл не найден: {image_path}")

    if win32 is None:
        raise RuntimeError(
            "Модуль pywin32 не найден. Установите: pip install pywin32 (только Windows)."
        )

    image_path = os.path.abspath(image_path)
    out_dir = os.path.dirname(image_path)
    base, _ = os.path.splitext(os.path.basename(image_path))
    out_format_upper = out_format.upper()
    if out_format_upper not in ("PNG", "JPEG", "AUTO"):
        raise ValueError("out_format должен быть PNG, JPEG или AUTO")
    # AUTO: выбирать формат по исходному расширению (PNG остаётся PNG, остальные — JPEG)
    if out_format_upper == "AUTO":
        _, orig_ext = os.path.splitext(image_path)
        if orig_ext.lower() == ".png":
            out_format_upper = "PNG"
        else:
            out_format_upper = "JPEG"
    out_name = f"{base}{out_suffix}.png" if out_format_upper == "PNG" else f"{base}{out_suffix}.jpg"
    out_path = os.path.join(out_dir, out_name)

    jsx_template = r"""
#target photoshop
app.bringToFront();

function saveAsPNG(doc, outPath) {
    var pngOptions = new PNGSaveOptions();
    pngOptions.interlaced = false;
    var outFile = new File(outPath);
    doc.saveAs(outFile, pngOptions, true, Extension.LOWERCASE);
}

function saveAsJPEG(doc, outPath, quality) {
    var jpgOptions = new JPEGSaveOptions();
    jpgOptions.quality = quality; // 0-12
    var outFile = new File(outPath);
    doc.saveAs(outFile, jpgOptions, true, Extension.LOWERCASE);
}

(function main() {
    var inPath = "%IN_PATH%";
    var outPath = "%OUT_PATH%";
    var sizeOrScale = %SIZE_OR_SCALE%;
    var mode = "%MODE%"; // 'scale' | 'fixed'
    var marginLeft = %MARGIN_LEFT%;
    var marginBottom = %MARGIN_BOTTOM%;
    var outFmt = "%OUT_FMT%"; // "PNG" или "JPEG"

    var prevRulerUnits = app.preferences.rulerUnits;
    var prevDialogs = app.displayDialogs;
    app.preferences.rulerUnits = Units.PIXELS;
    app.displayDialogs = DialogModes.NO; // отключить все диалоги

    var docOpenedHere = false;
    var doc;
    if (inPath && inPath.length > 0) {
        var f = new File(inPath);
        if (!f.exists) {
            throw new Error("Input file does not exist: " + inPath);
        }
        doc = app.open(f);
        docOpenedHere = true;
    } else {
        doc = app.activeDocument;
    }

    // Приводим к RGB 8 бит, затем конвертируем профиль к sRGB (если доступен)
    if (doc.mode !== DocumentMode.RGB) {
        doc.changeMode(ChangeMode.RGB);
    }
    try { doc.bitsPerChannel = BitsPerChannelType.EIGHT; } catch (e) {}
    try { doc.convertProfile("sRGB IEC61966-2.1", Intent.PERCEPTUAL, true, true); } catch (e) {}

    var w = doc.width.as('px');
    var h = doc.height.as('px');
    var minSide = Math.min(w, h);

    var size;
    if (mode === 'scale') {
        var divider = Math.max(1, sizeOrScale); // 3 => 1/3
        size = Math.floor(minSide / divider);
    } else { // fixed
        size = Math.min(minSide, Math.floor(sizeOrScale));
    }

    // Координаты квадрата: от правого нижнего угла, но с левым и нижним отступом
    // То есть квадрат прижимаем к правому нижнему, но сдвигаем вверх на marginBottom и влево от правого края на marginLeft
    var right = w - marginLeft;
    var bottom = h - marginBottom;
    var left = right - size;
    var top = bottom - size;

    // Границы не выходят за изображение
    if (left < 0) { right -= left; left = 0; }
    if (top < 0) { bottom -= top; top = 0; }

    var dup = doc.duplicate(doc.name + "_filled", true); // true = merge layers

    // Выделяем прямоугольную область
    var region = [
        [left, top],
        [right, top],
        [right, bottom],
        [left, bottom]
    ];
    dup.selection.select(region);

    // Выполняем заливку с учётом содержимого (Content-Aware Fill)
    try {
        var desc = new ActionDescriptor();
        desc.putEnumerated(
            stringIDToTypeID('usng'),
            stringIDToTypeID('fillContents'),
            stringIDToTypeID('contentAware')
        );
        desc.putUnitDouble(stringIDToTypeID('opacity'), stringIDToTypeID('percentUnit'), 100.0);
        desc.putEnumerated(stringIDToTypeID('mode'), stringIDToTypeID('blendMode'), stringIDToTypeID('normal'));
        desc.putBoolean(stringIDToTypeID('preserveTransparency'), false);
        // Цветовая адаптация (если поддерживается)
        try { desc.putInteger(stringIDToTypeID('contentAwareColorAdaptation'), 2); } catch (e) {}
        executeAction(stringIDToTypeID('fill'), desc, DialogModes.NO);
    } catch (e) {
        // Fallback на старую форму команды через charID
        try {
            var idFl = charIDToTypeID('Fl  ');
            var d2 = new ActionDescriptor();
            d2.putEnumerated(charIDToTypeID('Usng'), charIDToTypeID('FlCn'), stringIDToTypeID('contentAware'));
            d2.putUnitDouble(charIDToTypeID('Opct'), charIDToTypeID('#Prc'), 100.0);
            d2.putEnumerated(charIDToTypeID('Md  '), charIDToTypeID('BlnM'), charIDToTypeID('Nrml'));
            d2.putBoolean(charIDToTypeID('PrsT'), false);
            executeAction(idFl, d2, DialogModes.NO);
        } catch (e2) {}
    }

    // Снять выделение
    try { dup.selection.deselect(); } catch (e) {}

    if (dup.mode !== DocumentMode.RGB) { dup.changeMode(ChangeMode.RGB); }
    try { dup.bitsPerChannel = BitsPerChannelType.EIGHT; } catch (e) {}
    try { dup.convertProfile("sRGB IEC61966-2.1", Intent.PERCEPTUAL, true, true); } catch (e) {}

    if (outFmt === "PNG") {
        saveAsPNG(dup, outPath);
    } else {
        saveAsJPEG(dup, outPath, 10);
    }

    dup.close(SaveOptions.DONOTSAVECHANGES);
    if (docOpenedHere) { doc.close(SaveOptions.DONOTSAVECHANGES); }

    app.preferences.rulerUnits = prevRulerUnits;
    app.displayDialogs = prevDialogs;
})();
"""

    jsx_code = (
        jsx_template
        .replace("%IN_PATH%", image_path.replace("\\", "\\\\"))
        .replace("%OUT_PATH%", out_path.replace("\\", "\\\\"))
        .replace("%SIZE_OR_SCALE%", str(float(size_or_scale)))
        .replace("%MODE%", mode)
        .replace("%MARGIN_LEFT%", str(int(margin_left)))
        .replace("%MARGIN_BOTTOM%", str(int(margin_bottom)))
        .replace("%OUT_FMT%", out_format_upper)
    )

    created_here = False
    if ps is None:
        # Устойчивое создание COM-объекта Photoshop
        if pythoncom is not None:
            try:
                pythoncom.CoInitialize()
            except Exception:
                pass
        try:
            # Сначала пробуем подключиться к уже работающему экземпляру
            ps = win32.GetActiveObject("Photoshop.Application")
        except Exception:
            # Если не запущен — поднимаем новый
            try:
                if gencache is not None:
                    gencache.EnsureDispatch("Photoshop.Application")
                    ps = win32.Dispatch("Photoshop.Application")
                else:
                    ps = win32.Dispatch("Photoshop.Application")
            except Exception:
                ps = win32.DispatchEx("Photoshop.Application")
            created_here = True
    # Можно сделать невидимым: ps.Visible = False
    ps.Visible = True

    # Выполнить JSX-код непосредственно через Photoshop
    attempts = 2
    last_err = None
    for _ in range(attempts):
        try:
            ps.DoJavaScript(jsx_code)
            last_err = None
            break
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    if last_err:
        raise last_err

    return out_path, ps, created_here

def run_photoshop_batch(
    image_paths,
    size_or_scale: int | float = 3,
    out_suffix: str = "_filled",
    out_format: str = "PNG",
    mode: str = "scale",
    margin_left: int = 20,
    margin_bottom: int = 20,
):
    if win32 is None:
        raise RuntimeError("pywin32 не установлен. Установите: pip install pywin32")

    # Подготовим JSX один раз: он обработает массив путей в одном заходе
    out_format_upper = out_format.upper()
    if out_format_upper not in ("PNG", "JPEG", "AUTO"):
        raise ValueError("out_format должен быть PNG, JPEG или AUTO")

    # Сформируем массив путей для JSX
    parts = []
    for p in image_paths:
        abspath = os.path.abspath(p)
        abspath = abspath.replace("\\", "\\\\")  # экранируем обратные слеши для JSX-строки
        parts.append('"' + abspath + '"')
    js_array = ",".join(parts)

    jsx_template = r"""
#target photoshop
app.bringToFront();

function saveAsPNG(doc, outPath) {
    var pngOptions = new PNGSaveOptions();
    pngOptions.interlaced = false;
    var outFile = new File(outPath);
    doc.saveAs(outFile, pngOptions, true, Extension.LOWERCASE);
}

function saveAsJPEG(doc, outPath, quality) {
    var jpgOptions = new JPEGSaveOptions();
    jpgOptions.quality = quality; // 0-12
    var outFile = new File(outPath);
    doc.saveAs(outFile, jpgOptions, true, Extension.LOWERCASE);
}

function processOne(path, sizeOrScale, mode, marginLeft, marginBottom, outFmt, outSuffix) {
    var prevRulerUnits = app.preferences.rulerUnits;
    var prevDialogs = app.displayDialogs;
    app.preferences.rulerUnits = Units.PIXELS;
    app.displayDialogs = DialogModes.NO;

    try {
        var f = new File(path);
        if (!f.exists) { return null; }
        var doc = app.open(f);

        // Привести к RGB 8 бит и sRGB
        if (doc.mode !== DocumentMode.RGB) { doc.changeMode(ChangeMode.RGB); }
        try { doc.bitsPerChannel = BitsPerChannelType.EIGHT; } catch (e) {}
        try { doc.convertProfile("sRGB IEC61966-2.1", Intent.PERCEPTUAL, true, true); } catch (e) {}

        var w = doc.width.as('px');
        var h = doc.height.as('px');
        var minSide = Math.min(w, h);

        var size;
        if (mode === 'scale') {
            var divider = Math.max(1, sizeOrScale);
            size = Math.floor(minSide / divider);
        } else {
            size = Math.min(minSide, Math.floor(sizeOrScale));
        }

        var right = w - marginLeft;
        var bottom = h - marginBottom;
        var left = right - size;
        var top = bottom - size;
        if (left < 0) { right -= left; left = 0; }
        if (top < 0) { bottom -= top; top = 0; }

        // Дублируем и выполняем действия в suspendHistory (быстрее и чище история)
        var dup = doc.duplicate(doc.name + outSuffix, true);
        dup.suspendHistory("content aware fill", "(function(){\n" +
            "var region = [[" + left + "," + top + "],[" + right + "," + top + "],[" + right + "," + bottom + "],[" + left + "," + bottom + "]];\n" +
            "dup.selection.select(region);\n" +
            "try { var desc = new ActionDescriptor(); desc.putEnumerated(stringIDToTypeID('usng'), stringIDToTypeID('fillContents'), stringIDToTypeID('contentAware')); desc.putUnitDouble(stringIDToTypeID('opacity'), stringIDToTypeID('percentUnit'), 100.0); desc.putEnumerated(stringIDToTypeID('mode'), stringIDToTypeID('blendMode'), stringIDToTypeID('normal')); desc.putBoolean(stringIDToTypeID('preserveTransparency'), false); executeAction(stringIDToTypeID('fill'), desc, DialogModes.NO); } catch(e) {}\n" +) { try { var idFl = charIDToTypeID('Fl  '); var d2 = new ActionDescriptor(); d2.putEnumerated(charIDToTypeID('Usng'), charIDToTypeID('FlCn'), stringIDToTypeID('contentAware')); d2.putUnitDouble(charIDToTypeID('Opct'), charIDToTypeID('#Prc'), 100.0); d2.putEnumerated(charIDToTypeID('Md  '), charIDToTypeID('BlnM'), charIDToTypeID('Nrml')); d2.putBoolean(charIDToTypeID('PrsT'), false); executeAction(idFl, d2, DialogModes.NO); } catch(e2) {} }") { try { var idFl = charIDToTypeID('Fl  '); var d2 = new ActionDescriptor(); d2.putEnumerated(charIDToTypeID('Usng'), charIDToTypeID('FlCn'), stringIDToTypeID('contentAware')); d2.putUnitDouble(charIDToTypeID('Opct'), charIDToTypeID('#Prc'), 100.0); d2.putEnumerated(charIDToTypeID('Md  '), charIDToTypeID('BlnM'), charIDToTypeID('Nrml')); d2.putBoolean(charIDToTypeID('PrsT'), false); executeAction(idFl, d2, DialogModes.NO); } catch(e2) {} }\n" +
            "try{ dup.selection.deselect(); }catch(e){}\n" +
        "})()");

        if (dup.mode !== DocumentMode.RGB) { dup.changeMode(ChangeMode.RGB); }
        try { dup.bitsPerChannel = BitsPerChannelType.EIGHT; } catch (e) {}
        try { dup.convertProfile("sRGB IEC61966-2.1", Intent.PERCEPTUAL, true, true); } catch (e) {}

        var outDir = f.parent;
        var base = f.displayName.replace(/\.[^\.]+$/, "");
        var useFmt = outFmt;
        if (outFmt === 'AUTO') {
            var lower = path.toLowerCase();
            if (lower.endsWith('.png')) {
                useFmt = 'PNG';
            } else if (lower.endsWith('.jpg') || lower.endsWith('.jpeg')) {
                useFmt = 'JPEG';
            } else {
                useFmt = 'PNG';
            }
        }
        var outName = base + outSuffix + (useFmt === 'PNG' ? '.png' : '.jpg');
        var outFile = new File(outDir + "/" + outName);

        if (useFmt === 'PNG') {
            saveAsPNG(dup, outFile.fsName);
        } else {
            try { dup.flatten(); } catch(e) {}
            saveAsJPEG(dup, outFile.fsName, 10);
        }

        dup.close(SaveOptions.DONOTSAVECHANGES);
        doc.close(SaveOptions.DONOTSAVECHANGES);
    } catch (err) {
        // Игнорируем, чтобы не прерывать батч
    } finally {
        app.preferences.rulerUnits = prevRulerUnits;
        app.displayDialogs = prevDialogs;
    }
}

function processOne2(path, sizeOrScale, mode, marginLeft, marginBottom, outFmt, outSuffix) {
    var prevRulerUnits = app.preferences.rulerUnits;
    var prevDialogs = app.displayDialogs;
    app.preferences.rulerUnits = Units.PIXELS;
    app.displayDialogs = DialogModes.NO;
    try {
        var f = new File(path);
        if (!f.exists) { return null; }
        var doc = app.open(f);

        if (doc.mode !== DocumentMode.RGB) { doc.changeMode(ChangeMode.RGB); }
        try { doc.bitsPerChannel = BitsPerChannelType.EIGHT; } catch (e) {}
        try { doc.convertProfile("sRGB IEC61966-2.1", Intent.PERCEPTUAL, true, true); } catch (e) {}

        var w = doc.width.as('px');
        var h = doc.height.as('px');
        var minSide = Math.min(w, h);
        var size;
        if (mode === 'scale') {
            var divider = Math.max(1, sizeOrScale);
            size = Math.floor(minSide / divider);
        } else {
            size = Math.min(minSide, Math.floor(sizeOrScale));
        }
        var right = w - marginLeft;
        var bottom = h - marginBottom;
        var left = right - size;
        var top = bottom - size;
        if (left < 0) { right -= left; left = 0; }
        if (top < 0) { bottom -= top; top = 0; }

        var dup = doc.duplicate(doc.name + outSuffix, true);
        app.activeDocument = dup;
        var region = [ [left, top], [right, top], [right, bottom], [left, bottom] ];
        dup.selection.select(region);
        try {
            var desc = new ActionDescriptor();
            desc.putEnumerated(stringIDToTypeID('usng'), stringIDToTypeID('fillContents'), stringIDToTypeID('contentAware'));
            desc.putUnitDouble(stringIDToTypeID('opacity'), stringIDToTypeID('percentUnit'), 100.0);
            desc.putEnumerated(stringIDToTypeID('mode'), stringIDToTypeID('blendMode'), stringIDToTypeID('normal'));
            desc.putBoolean(stringIDToTypeID('preserveTransparency'), false);
            try { desc.putInteger(stringIDToTypeID('contentAwareColorAdaptation'), 2); } catch (e) {}
            executeAction(stringIDToTypeID('fill'), desc, DialogModes.NO);
        } catch (e) {
            try {
                var idFl = charIDToTypeID('Fl  ');
                var d2 = new ActionDescriptor();
                d2.putEnumerated(charIDToTypeID('Usng'), charIDToTypeID('FlCn'), stringIDToTypeID('contentAware'));
                // Some builds expect '#Prc' vs 'Prc ' for percent
                try { d2.putUnitDouble(charIDToTypeID('Opct'), charIDToTypeID('#Prc'), 100.0); } catch (e3) { d2.putUnitDouble(charIDToTypeID('Opct'), charIDToTypeID('Prc '), 100.0); }
                d2.putEnumerated(charIDToTypeID('Md  '), charIDToTypeID('BlnM'), charIDToTypeID('Nrml'));
                d2.putBoolean(charIDToTypeID('PrsT'), false);
                executeAction(idFl, d2, DialogModes.NO);
            } catch (e2) {}
        }
        try { dup.selection.deselect(); } catch (e) {}

        if (dup.mode !== DocumentMode.RGB) { dup.changeMode(ChangeMode.RGB); }
        try { dup.bitsPerChannel = BitsPerChannelType.EIGHT; } catch (e) {}
        try { dup.convertProfile("sRGB IEC61966-2.1", Intent.PERCEPTUAL, true, true); } catch (e) {}

        var outDir = f.parent;
        var base = f.displayName.replace(/\.[^\.]+$/, "");
        var useFmt = outFmt;
        if (outFmt === 'AUTO') {
            var lower = path.toLowerCase();
            if (lower.endsWith('.png')) {
                useFmt = 'PNG';
            } else if (lower.endsWith('.jpg') || lower.endsWith('.jpeg')) {
                useFmt = 'JPEG';
            } else {
                useFmt = 'PNG';
            }
        }
        var outName = base + outSuffix + (useFmt === 'PNG' ? '.png' : '.jpg');
        var outFile = new File(outDir + "/" + outName);
        if (useFmt === 'PNG') {
            saveAsPNG(dup, outFile.fsName);
        } else {
            try { dup.flatten(); } catch(e) {}
            saveAsJPEG(dup, outFile.fsName, 10);
        }
        dup.close(SaveOptions.DONOTSAVECHANGES);
        doc.close(SaveOptions.DONOTSAVECHANGES);
    } catch (err) {
        // swallow to keep batch going
    } finally {
        app.preferences.rulerUnits = prevRulerUnits;
        app.displayDialogs = prevDialogs;
    }
}

(function main(){
    var paths = [%PATHS_ARRAY%];
    var sizeOrScale = %SIZE_OR_SCALE%;
    var mode = "%MODE%";
    var marginLeft = %MARGIN_LEFT%;
    var marginBottom = %MARGIN_BOTTOM%;
    var outFmt = "%OUT_FMT%";
    var outSuffix = "%OUT_SUFFIX%";

    for (var i=0;i<paths.length;i++) {
        processOne2(paths[i], sizeOrScale, mode, marginLeft, marginBottom, outFmt, outSuffix);
    }
})();
"""

    jsx_code = (
        jsx_template
        .replace("%PATHS_ARRAY%", js_array)
        .replace("%SIZE_OR_SCALE%", str(float(size_or_scale)))
        .replace("%MODE%", mode)
        .replace("%MARGIN_LEFT%", str(int(margin_left)))
        .replace("%MARGIN_BOTTOM%", str(int(margin_bottom)))
        .replace("%OUT_FMT%", out_format_upper)
        .replace("%OUT_SUFFIX%", out_suffix)
    )

    # Инициализация Photoshop один раз
    if pythoncom is not None:
        try:
            pythoncom.CoInitialize()
        except Exception:
            pass
    ps = None
    created_here = False
    try:
        ps = win32.GetActiveObject("Photoshop.Application")
    except Exception:
        try:
            if gencache is not None:
                gencache.EnsureDispatch("Photoshop.Application")
                ps = win32.Dispatch("Photoshop.Application")
            else:
                ps = win32.Dispatch("Photoshop.Application")
        except Exception:
            ps = win32.DispatchEx("Photoshop.Application")
        created_here = True

    ps.Visible = True

    # Override JSX with a safe template that saves beside source and never modifies originals
    jsx_code = (r"""
#target photoshop
app.bringToFront();

function saveAsPNG(doc, outPath) {
    var pngOptions = new PNGSaveOptions();
    pngOptions.interlaced = false;
    var outFile = new File(outPath);
    doc.saveAs(outFile, pngOptions, true, Extension.LOWERCASE);
}

function saveAsJPEG(doc, outPath, quality) {
    var jpgOptions = new JPEGSaveOptions();
    jpgOptions.quality = quality; // 0-12
    var outFile = new File(outPath);
    doc.saveAs(outFile, jpgOptions, true, Extension.LOWERCASE);
}

function processOne2(path, sizeOrScale, mode, marginLeft, marginBottom, outFmt, outSuffix) {
    var prevRulerUnits = app.preferences.rulerUnits;
    var prevDialogs = app.displayDialogs;
    app.preferences.rulerUnits = Units.PIXELS;
    app.displayDialogs = DialogModes.NO;
    try {
        // Defensive cleanup: if a previous iteration left documents open (due to an error/modal state),
        // close everything so the batch can continue instead of getting stuck.
        try {
            while (app.documents.length > 0) {
                try { app.activeDocument.close(SaveOptions.DONOTSAVECHANGES); } catch(e_close) { break; }
            }
        } catch(e_cleanup) {}

        var f = new File(path);
        if (!f.exists) { return null; }
        var doc = app.open(f);

        // Дублируем сразу, оригинал не трогаем
        var dup = doc.duplicate(doc.name + outSuffix, true);
        app.activeDocument = dup;
        try { doc.close(SaveOptions.DONOTSAVECHANGES); } catch(e_close) {}

        // Привести дубликат к RGB 8 бит и sRGB
        if (dup.mode !== DocumentMode.RGB) { dup.changeMode(ChangeMode.RGB); }
        try { dup.bitsPerChannel = BitsPerChannelType.EIGHT; } catch (e) {}
        try { dup.convertProfile("sRGB IEC61966-2.1", Intent.PERCEPTUAL, true, true); } catch (e) {}

        var w = dup.width.as('px');
        var h = dup.height.as('px');
        var minSide = Math.min(w, h);
        var size;
        if (mode === 'scale') {
            var divider = Math.max(1, sizeOrScale);
            size = Math.floor(minSide / divider);
        } else {
            size = Math.min(minSide, Math.floor(sizeOrScale));
        }
        size = Math.max(1, size);
        var right = w - marginLeft;
        var bottom = h - marginBottom;
        var left = right - size;
        var top = bottom - size;
        if (left < 0) { right -= left; left = 0; }
        if (top < 0) { bottom -= top; top = 0; }
        if (right - left < 1 || bottom - top < 1) {
            // Нечего выделять — пропускаем файл
            try { dup.close(SaveOptions.DONOTSAVECHANGES); } catch(e_skip) {}
            return null;
        }

        // Выделение и заливка с учётом содержимого
        try {
            var region = [ [left, top], [right, top], [right, bottom], [left, bottom] ];
            dup.selection.select(region);
        } catch(selErr) {}
        try {
            var desc = new ActionDescriptor();
            desc.putEnumerated(stringIDToTypeID('usng'), stringIDToTypeID('fillContents'), stringIDToTypeID('contentAware'));
            desc.putUnitDouble(stringIDToTypeID('opacity'), stringIDToTypeID('percentUnit'), 100.0);
            desc.putEnumerated(stringIDToTypeID('mode'), stringIDToTypeID('blendMode'), stringIDToTypeID('normal'));
            desc.putBoolean(stringIDToTypeID('preserveTransparency'), false);
            try { desc.putInteger(stringIDToTypeID('contentAwareColorAdaptation'), 2); } catch (e) {}
            executeAction(stringIDToTypeID('fill'), desc, DialogModes.NO);
        } catch (e) {
            try {
                var idFl = charIDToTypeID('Fl  ');
                var d2 = new ActionDescriptor();
                d2.putEnumerated(charIDToTypeID('Usng'), charIDToTypeID('FlCn'), stringIDToTypeID('contentAware'));
                try { d2.putUnitDouble(charIDToTypeID('Opct'), charIDToTypeID('#Prc'), 100.0); } catch (e3) { d2.putUnitDouble(charIDToTypeID('Opct'), charIDToTypeID('Prc '), 100.0); }
                d2.putEnumerated(charIDToTypeID('Md  '), charIDToTypeID('BlnM'), charIDToTypeID('Nrml'));
                d2.putBoolean(charIDToTypeID('PrsT'), false);
                executeAction(idFl, d2, DialogModes.NO);
            } catch (e2) {}
        }
        try { dup.selection.deselect(); } catch (e) {}

        var outDirPath = new File(path).parent.fsName;
        var base = f.displayName.replace(/\.[^\.]+$/, "");
        var useFmt = outFmt;
        if (outFmt === 'AUTO') {
            var lower = path.toLowerCase();
            if (lower.endsWith('.png')) {
                useFmt = 'PNG';
            } else if (lower.endsWith('.jpg') || lower.endsWith('.jpeg')) {
                useFmt = 'JPEG';
            } else {
                // Для неизвестных/нестандартных форматов (webp, tiff, psd, bmp и т.д.) — сохраняем как PNG
                useFmt = 'PNG';
            }
        }
        var outName = base + outSuffix + (useFmt === 'PNG' ? '.png' : '.jpg');
        var outFile = new File(outDirPath + "/" + outName);
        if (useFmt === 'PNG') {
            saveAsPNG(dup, outFile.fsName);
        } else {
            try { dup.flatten(); } catch(e) {}
            saveAsJPEG(dup, outFile.fsName, 10);
        }

        try { dup.close(SaveOptions.DONOTSAVECHANGES); } catch(e_finish) {}
    } catch (err) {
        // Defensive cleanup: close any documents that might have stayed open due to errors.
        try {
            while (app.documents.length > 0) {
                try { app.activeDocument.close(SaveOptions.DONOTSAVECHANGES); } catch(e_close2) { break; }
            }
        } catch(e_cleanup2) {}
        // swallow to keep batch going
    } finally {
        // Reduce scratch disk usage: clear caches periodically to avoid "primary scratch disk full".
        try {
            app.purge(PurgeTarget.ALLCACHES);
        } catch (e_purge) {
            try { app.purge(PurgeTarget.UNDOCACHES); } catch (e_purge2) {}
        }
        app.preferences.rulerUnits = prevRulerUnits;
        app.displayDialogs = prevDialogs;
    }
}

(function main(){
    var paths = [%PATHS_ARRAY%];
    var sizeOrScale = %SIZE_OR_SCALE%;
    var mode = "%MODE%";
    var marginLeft = %MARGIN_LEFT%;
    var marginBottom = %MARGIN_BOTTOM%;
    var outFmt = "%OUT_FMT%";
    var outSuffix = "%OUT_SUFFIX%";

    for (var i=0;i<paths.length;i++) {
        processOne2(paths[i], sizeOrScale, mode, marginLeft, marginBottom, outFmt, outSuffix);
    }
})();
"""
        .replace("%PATHS_ARRAY%", js_array)
        .replace("%SIZE_OR_SCALE%", str(float(size_or_scale)))
        .replace("%MODE%", mode)
        .replace("%MARGIN_LEFT%", str(int(margin_left)))
        .replace("%MARGIN_BOTTOM%", str(int(margin_bottom)))
        .replace("%OUT_FMT%", out_format_upper)
        .replace("%OUT_SUFFIX%", out_suffix)
    )

    # Тёплый старт: даём Photoshop договорузиться перед основным скриптом
    for _ in range(10):
        try:
            ps.DoJavaScript("1;")
            break
        except Exception:
            time.sleep(0.5)

    attempts = 3
    last_err = None
    for _ in range(attempts):
        try:
            ps.DoJavaScript(jsx_code)
            last_err = None
            break
        except Exception as e:
            last_err = e
            time.sleep(0.75)
    if last_err:
        raise last_err

    return created_here, ps

    print(f"Готово! Файл сохранён: {out_path}")
    return out_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python photoshop_crop_bottom_right.py <путь_к_изображению>[,ещё...] [size_or_scale] [format=PNG|JPEG] [mode=fixed|scale] [margin_left] [margin_bottom]")
        print("Примеры:\n  python photoshop_crop_bottom_right.py C:\\img1.jpg C:\\img2.jpg\n  python photoshop_crop_bottom_right.py C:\\img1.jpg C:\\img2.jpg 3 PNG scale 20 20")
        sys.exit(1)

    # Соберём входные пути: все позиционные аргументы до первого, который совпадает с возможными значениями формата/режима или выглядит как число
    args = sys.argv[1:]
    img_paths = []
    rest = []
    for a in args:
        if os.path.exists(a) and (a.lower().endswith(('.jpg','.jpeg','.png','.tif','.tiff','.webp','.bmp','.psd'))):
            img_paths.append(a)
        else:
            rest.append(a)
    if not img_paths:
        # fallback: если пути содержат пробелы и не прошли exists, всё равно попробуем взять первый параметр как путь
        img_paths = [args[0]]
        rest = args[1:]

    # Параметры обработки
    size_or_scale = float(rest[0]) if len(rest) >= 1 and rest[0].replace('.', '', 1).isdigit() else 3
    fmt = rest[1] if len(rest) >= 2 else "PNG"
    mode = rest[2] if len(rest) >= 3 else "scale"
    margin_left = int(rest[3]) if len(rest) >= 4 else 20
    margin_bottom = int(rest[4]) if len(rest) >= 5 else 20

    # Батч одним вызовом JSX — быстрее и стабильнее
    created_here, ps = run_photoshop_batch(
        img_paths,
        size_or_scale=size_or_scale,
        out_suffix="_filled",
        out_format=fmt,
        mode=mode,
        margin_left=margin_left,
        margin_bottom=margin_bottom,
    )

    # Закрывать Photoshop НЕ будем. По окончанию просто закроем все открытые документы без сохранения,
    # чтобы приложение осталось запущенным, как просили.
    try:
        if ps is not None:
            try:
                # 2 соответствует SaveOptions.DONOTSAVECHANGES в COM
                while True:
                    try:
                        if ps.Documents.Count == 0:
                            break
                        ps.ActiveDocument.Close(2)
                    except Exception:
                        break
            except Exception:
                pass
    except Exception:
        pass

    print("Готово! Обработано файлов: ", len(img_paths))
