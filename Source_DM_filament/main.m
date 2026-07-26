clear
clc
close all

global lineN layerN Linfo

fD = 1.75; % filament diameter
h = 0.125; % layer height
layerN = fD/h;
lineN = 48;
rat = (1.75/0.4)^2;
zref = layerN/2*h;
Linfo = [2; 5; 8; 12; 16; 20; 24; 28; 32; 36; 40; 43; 46; 48];

ds = 0.01; % resolution, does not affect processing speed or gcode size
dp = 0.4/ds; % unit distance of G1, recommend 0.4/ds, affects processing speed and gcode size
pit = fD + 0.4; %fD + 0.4; % increment per rev, pitch - fD+0.4 for max, fD+1 for small amount
a =  50;% 40; % inner radius - 40 for max, 60 for small amount
move_origin = [250 210]/2;
interfdist = 0.15; % distance between material sections, in mm


% printSetting = 1.material num, 2.printF, 3.travelF, 4.retractOn,
% 5.retractF, 6.retractF(Z), 7.retractE, 8.retractZ, 9.Flow rate, 10.temp,
% 11.cooling fan on
printSetting = [ 1 2400 6000 1 2400 600 2 2 1.02 225 1;        % PLA
                 2 2400 6000 1 2400 600 2 2 1.02 220 1;        % CPLA
                 3 1800 6000 1 1800 600 1.2 2 1.1 220 0;    % TPU
                 4 1800 6000 1 2400 600 1.6 2 1.02 240 0;        % PETG
                 5 1800 6000 1 2400 600 1.2 2 1.02 200 0;  % SMP
                 100 2400 6000 1 2400 600 2 2 1.02 220 1;       % CYAN
                 200 2400 6000 1 2400 600 2 2 1.02 225 1;       % MAGENTA
                 300 2400 6000 1 2400 600 2 2 1.02 220 1;       % YELLOW
                 400 2400 6000 1 2400 600 2 2 1.02 220 1;       % WHITE
                 500 2400 6000 1 2400 600 2 2 1.02 220 1;       % BLACK
                 ];
vidOn = "on";  %on/off

sourceDmOutputDir = getenv('B_FDM_SOURCE_DM_OUTPUT_DIR');
if isempty(sourceDmOutputDir)
    sourceDmOutputDir = pwd;
end
if ~exist(sourceDmOutputDir, 'dir')
    mkdir(sourceDmOutputDir);
end
gcodeSaveDir = sourceDmOutputDir;
vidSaveDir = gcodeSaveDir;
[~, outputDirName, ~] = fileparts(sourceDmOutputDir);
if exist('fileNameDateOverride', 'var') && ~isempty(fileNameDateOverride)
    fileNameDate = string(fileNameDateOverride);
else
    fileNameDate = getenv('B_FDM_SOURCE_DM_OUTPUT_BASENAME');
end
if isempty(fileNameDate) || strcmp(string(fileNameDate), "source_dm_filament")
    if isempty(outputDirName)
        fileNameDate = string(datetime('now','Format','yyMMdd_HHmm'));
    else
        fileNameDate = string(outputDirName);
    end
else
    fileNameDate = string(fileNameDate);
end
feedLength_start = 130;  % Extra length which is removable(compensate poor quality of the starting point)
feedLength_end = 10;   % Extra length to ensure that the end of the programmed part is extruded

%Matinfo start
sourceDmMatinfoPath = getenv('B_FDM_SOURCE_DM_MATINFO_PATH');
if isempty(sourceDmMatinfoPath)
    sourceDmMatinfoPath = 'Output_Results\Matinfo_model_output.mat';
end
load(sourceDmMatinfoPath)
%Matinfo end

Ti = [Ti(:,1), round(Ti(:,2)/ds,0), zeros(size(Ti,1),1)];
Ti(1,3)=Ti(1,2);
for i = 2:size(Ti,1)    
    Ti(i,3) = Ti(i-1,3)+Ti(i,2);
end
% Ti = [material num, length, accumulative length]
Sg = segGen(Ti, Tdef);

if ~exist('po','var') || isempty(po)
    po = printingOrderAuto(Ti,Tdef,Sg);
end

Seg = segArr(Sg,po);
if isempty(Seg)
    warning('Loaded po produced no valid segments. Regenerating po automatically from Ti/Tdef/Sg.');
    po = printingOrderAuto(Ti,Tdef,Sg);
    Seg = segArr(Sg,po);
end
if isempty(Seg)
    error('No valid segments could be generated from the loaded or auto-generated printing order.');
end
% interdist in seg2vec defines gap between heterogeneous sections
V = seg2vec(Seg, ds, interfdist);


% Filament point cloud generator
numPoints = max(V(:,3))+3;
filamentLength = max(V(:,3))*ds; % total filament length[mm];

spiralRef = spiralPoints(ds,pit,a,numPoints,move_origin);

filaCloud = filaPoints(spiralRef, lineN, h, zref, move_origin);
% P(i,1) = {[Ln, x, y, r, phi, z, w]};

%generate Gcode path
Path = pathGen(V, filaCloud, dp, po);
disp('Path generated')

startCode = {
    'M82 ;absolute extrusion mode';
    'G21 ; set units to millimeters';
    'G90 ; use absolute positioning';
    'M82 ; absolute extrusion mode';
    'M104 S225 ; set extruder temp';
    'M140 S70 ; set bed temp';
    'M190 S70 ; wait for bed temp';
    'M109 S225 ; wait for extruder temp';
    'G28 W ; home all without mesh bed level';
    'G80 ; mesh bed leveling';
    'M907 E400';
    'M204 S600';
    'G92 E0.0 ; reset extruder distance position';
    'G1 Y-3.0 F1000.0 ; go outside print area';
    'G1 X60.0 E9.0 F1000.0 ; intro line';
    'G1 X100.0 E15 F1000.0 ; intro line';
    'G92 E0.0 ; reset extruder distance position';
    'G92 E0';
    'G1 F1200 E-1';
    'M107' };

endCode = {
    'G92 E0';
    'G92 E0';
    'G1 F600 Z15';
    'M107';
    'G92 E0;';
    'M104 S0 ; turn off extruder';
    'M140 S0 ; turn off heatbed';
    'M107 ; turn off fan';
    'G1 X0 Y210; home X axis and push Y forward';
    'M84 ; disable motors';
    'M82 ;absolute extrusion mode';
    'M104 S0';
    ';End of Gcode' };

% write Gcode 
Gcode = gcodeWrite(Path,h,fD, printSetting);
GcodeAll = cat(1,startCode{:},cat(1,Gcode{:}),endCode{:});

% save Gcode
fout = fopen(strcat(gcodeSaveDir,'\',fileNameDate,'_mod.txt'),'w');
fprintf(fout,'%s\n',GcodeAll{:});
fclose(fout);
disp('Gcode saved')

[~] = preview(V, Path, vidOn, vidSaveDir, fileNameDate, ds);


