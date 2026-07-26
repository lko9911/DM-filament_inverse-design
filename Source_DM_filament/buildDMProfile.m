function DM = buildDMProfile(DMtable)
% DMtable = [s_start, s_end, matID]
% 또는     = [s_start, s_end, matID, featureValue]

    if size(DMtable,2) < 3
        error('DMtable must have at least 3 columns: [start, end, matID]');
    end

    DM = struct('s1',{},'s2',{},'matID',{},'feature',{});

    for i = 1:size(DMtable,1)
        DM(i).s1 = DMtable(i,1);
        DM(i).s2 = DMtable(i,2);
        DM(i).matID = DMtable(i,3);

        if size(DMtable,2) >= 4
            DM(i).feature = DMtable(i,4);
        else
            DM(i).feature = DMtable(i,3); % 기본은 matID와 같게
        end
    end
end