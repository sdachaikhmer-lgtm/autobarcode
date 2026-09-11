@echo off
setlocal

for /f "tokens=2-4 delims=/ " %%a in ('date /t') do (
    set m=%%a
    set d=%%b
    set y=%%c
)
if "%m%"=="01" set mname=Jan
if "%m%"=="02" set mname=Feb
if "%m%"=="03" set mname=Mar
if "%m%"=="04" set mname=Apr
if "%m%"=="05" set mname=May
if "%m%"=="06" set mname=Jun
if "%m%"=="07" set mname=Jul
if "%m%"=="08" set mname=Aug
if "%m%"=="09" set mname=Sep
if "%m%"=="10" set mname=Oct
if "%m%"=="11" set mname=Nov
if "%m%"=="12" set mname=Dec

set "TODAY_FOLDER=D:\Scan\Main OutputFastOCR\OutputFastOCR_%d%-%mname%-%y%"

echo Creating date-based folder structure for: %TODAY_FOLDER%

mkdir "%TODAY_FOLDER%\All output images for another process" >nul 2>&1
mkdir "%TODAY_FOLDER%\BarcodeandStamp" >nul 2>&1
mkdir "%TODAY_FOLDER%\Container List" >nul 2>&1
mkdir "%TODAY_FOLDER%\Container Match Format" >nul 2>&1
mkdir "%TODAY_FOLDER%\Main" >nul 2>&1
mkdir "%TODAY_FOLDER%\Missing Format" >nul 2>&1
mkdir "%TODAY_FOLDER%\No Seal" >nul 2>&1
mkdir "%TODAY_FOLDER%\Not Under GHP" >nul 2>&1
mkdir "%TODAY_FOLDER%\Part" >nul 2>&1
mkdir "%TODAY_FOLDER%\Report" >nul 2>&1

cd /d "D:\barcodestame\code for Auto barcod and stamp\dist"
app.exe
pause