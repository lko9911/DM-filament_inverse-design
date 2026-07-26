function Path = pathGen(V, filaCloud, dp, po)
% vec = [material num, startpoint, endpoint, layer num, startline, endline]

Path = cell(size(V,1),3);
for i = 1:size(Path,1)    
    Pts = (V(i,2):dp:V(i,3))';
    if Pts(end) ~= V(i,3)
        Pts(end+1) = V(i,3);
    end    
    Ls = (V(i,5):1:V(i,6))';
    if size(Ls,1) == 1
        Sa = filaCloud{Ls(1),1}(Pts,:);
        Sb=[]; Sc=[]; Sd=[]; Sia=[]; Sib=[];
    elseif size(Ls,1) > 1
        Sa = filaCloud{Ls(1),1}(Pts,:);
        Sc = filaCloud{Ls(2),1}(flipud(Pts(1:end)),:);
        if size(Ls,1) == 3
            Sia = filaCloud{Ls(3),1}(Pts(1:end-1),:);
            Sib = [];
        elseif size(Ls,1) == 4
            Sia = filaCloud{Ls(3),1}(Pts(1:end-1),:);
            Sib = filaCloud{Ls(4),1}(flipud(Pts(2:end-1)),:);
        else
            Sia = [];
            Sib = [];
        end
    end
    Path(i,1) = { [Sa; Sc; Sia; Sib] };
    Path(i,2) = filaCloud(Ls(1),2);
    Path(i,3) = {V(i,1)};
end

end
