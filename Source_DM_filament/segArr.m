function Seg = segArr(Sg,po)
% Seg(in) = [material number, startpoint, endpoint, line number];
% Seg(out) = [printing order, startpoint, endpoint, startline, endline of the interval];

c = 1;
Seg = zeros(1, size(Sg,2));
for i=1:size(po,1)
    pm = po(i,1);
    pl = po(i,2):po(i,3);
    Sg_temp = Sg( ismember(Sg(:,1),pm,'rows') ,: );
    Sg_temp = Sg_temp( ismember(Sg_temp(:,4),pl), :);
    Sg_temp = sortrows( Sg_temp,4 );
    Seg = [Seg; Sg_temp];
end
Seg(1,:) = [];

end