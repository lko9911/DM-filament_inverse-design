function co = mn2c(mnum)

alpha = 1;
if mnum == 1
    co = [225 50 50]; 
elseif mnum == 2
    co = [0 0 0];%black
elseif mnum == 3
    co =  [191 191 191]; %[255 200 200]/255; %pink
elseif mnum == 4
    co = [77 190 238]; % '#4DBEEE';%blue
elseif mnum == 5
    co = [26 225 220]; 
elseif mnum == 100
    co = [40 120 180]; %[26 225 220]; % '#1AE1DC'
elseif mnum == 200
    co = [210 60 80]; %[255 105 255]; % '#FF69FF'
elseif mnum == 300
    co = [240 220 40]; %[224 224 11]; % '#E0E00B';
elseif mnum == 400
    co = [210 210 210]; % '#BFBFBF';
elseif mnum == 500
    co = [0 0 0]; % BLACK
else
    error('No preview color is defined for material code %d.', mnum);
end
co = [co/255 alpha];

% '#77AC30' yellow
